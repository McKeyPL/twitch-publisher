from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from config import RetryConfig, YouTubeConfig
from state import StateStore
from uploaders.youtube import YouTubeUploader, _pacific_quota_window


@pytest.fixture
def youtube_config(tmp_path: Path) -> YouTubeConfig:
    return YouTubeConfig(
        enabled=True,
        client_secrets_file=tmp_path / "credentials.json",
        token_file=tmp_path / "auth" / "token.json",
        privacy_status="unlisted",
        max_duration_hours=12,
        title_limit=100,
        category_id="20",
        captions_language="pl",
        captions_name="Twitch Chat",
        daily_upload_limit=100,
        daily_quota_units=10_000,
        upload_quota_units=1,
        captions_quota_units=400,
        srt_max_size_mb=100,
        playlists={"mrozopl": ""},
    )


@pytest.fixture
def retry_config() -> RetryConfig:
    return RetryConfig(
        max_attempts=3,
        initial_backoff_seconds=0.01,
        multiplier=2,
        max_backoff_seconds=0.02,
    )


def make_video(tmp_path: Path) -> Path:
    video = tmp_path / "stream.mkv"
    video.write_bytes(b"fake-video")
    return video


def successful_video_service(video_id: str = "video123") -> MagicMock:
    service = MagicMock()
    upload_request = MagicMock()
    upload_request.next_chunk.return_value = (None, {"id": video_id})
    service.videos.return_value.insert.return_value = upload_request
    return service


def test_service_is_built_lazily_with_mocked_discovery_build(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    service = MagicMock()
    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        with (
            patch.object(uploader, "_get_credentials", return_value=MagicMock()) as credentials,
            patch("uploaders.youtube.build", return_value=service) as mocked_build,
        ):
            assert uploader._get_service() is service
            assert uploader._get_service() is service

    credentials.assert_called_once()
    mocked_build.assert_called_once_with(
        "youtube",
        "v3",
        credentials=credentials.return_value,
        cache_discovery=False,
    )


def test_successful_resumable_upload(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    video = make_video(tmp_path)
    service = successful_video_service()

    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        uploader._service = service
        with patch("uploaders.youtube.MediaFileUpload") as media_upload:
            result = uploader.upload(
                video,
                "Title",
                "Description",
                ["mrozopl", "Twitch"],
            )

        period, _ = _pacific_quota_window()
        assert store.get_quota_usage("youtube_videos_insert", period) == 1

    assert result.success is True
    assert result.platform_video_id == "video123"
    assert result.platform_url == "https://youtube.com/watch?v=video123"
    assert result.captions_uploaded is False
    media_upload.assert_called_once()
    assert media_upload.call_args.kwargs["resumable"] is True
    assert media_upload.call_args.kwargs["chunksize"] == 50 * 1024 * 1024
    insert_kwargs = service.videos.return_value.insert.call_args.kwargs
    assert insert_kwargs["body"]["snippet"]["categoryId"] == "20"
    assert insert_kwargs["body"]["status"]["privacyStatus"] == "unlisted"


def test_quota_is_rejected_before_service_or_upload_is_created(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    video = make_video(tmp_path)
    with StateStore(tmp_path / "state.sqlite3") as store:
        period, _ = _pacific_quota_window()
        assert store.try_reserve_quota("youtube_videos_insert", period, 1, 1)[0]
        uploader = YouTubeUploader(
            replace(youtube_config, daily_upload_limit=1), retry_config, store
        )
        with (
            patch("uploaders.youtube.build") as mocked_build,
            patch("uploaders.youtube.MediaFileUpload") as media_upload,
        ):
            result = uploader.upload(video, "Title", "Description", [])

    assert result.success is False
    assert "reset at Pacific Time midnight" in (result.error_message or "")
    mocked_build.assert_not_called()
    media_upload.assert_not_called()


def test_retries_next_chunk_after_http_500(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    video = make_video(tmp_path)
    response = httplib2.Response({"status": "500"})
    transient_error = HttpError(
        response,
        b'{"error":{"errors":[{"reason":"backendError"}]}}',
    )
    service = MagicMock()
    upload_request = MagicMock()
    upload_request.next_chunk.side_effect = [
        transient_error,
        (None, {"id": "after-retry"}),
    ]
    service.videos.return_value.insert.return_value = upload_request

    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        uploader._service = service
        with (
            patch("uploaders.youtube.MediaFileUpload"),
            patch("uploaders.base.time.sleep") as sleep,
        ):
            result = uploader.upload(video, "Title", "Description", [])

    assert result.success is True
    assert result.platform_video_id == "after-retry"
    assert upload_request.next_chunk.call_count == 2
    sleep.assert_called_once_with(0.01)


def test_uploads_captions_after_video(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    video = make_video(tmp_path)
    srt = tmp_path / "stream_chat.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nChat\n", encoding="utf-8")
    service = successful_video_service("with-captions")
    captions_request = MagicMock()
    captions_request.execute.return_value = {"id": "caption-id"}
    service.captions.return_value.insert.return_value = captions_request

    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        uploader._service = service
        with patch("uploaders.youtube.MediaFileUpload"):
            result = uploader.upload(video, "Title", "Description", [], srt)

        period, _ = _pacific_quota_window()
        assert store.get_quota_usage("youtube_videos_insert", period) == 1
        assert store.get_quota_usage("youtube_general", period) == 400

    assert result.success is True
    assert result.captions_uploaded is True
    assert result.error_message is None
    caption_body = service.captions.return_value.insert.call_args.kwargs["body"]
    assert caption_body["snippet"]["videoId"] == "with-captions"
    assert caption_body["snippet"]["language"] == "pl"


def test_creates_playlist_when_identifier_is_empty(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = MagicMock()
    create_request = MagicMock()
    create_request.execute.return_value = {"id": "new-playlist-id"}
    service.playlists.return_value.insert.return_value = create_request
    item_request = MagicMock()
    item_request.execute.return_value = {"id": "playlist-item-id"}
    service.playlistItems.return_value.insert.return_value = item_request

    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        uploader._service = service
        with caplog.at_level("WARNING"):
            added = uploader.add_to_playlist(
                "video123",
                "",
                playlist_title="mrozopl",
            )

    assert added is True
    assert "YT_PLAYLIST_MROZOPL=new-playlist-id" in caplog.text
    playlist_item_body = service.playlistItems.return_value.insert.call_args.kwargs["body"]
    assert playlist_item_body["snippet"]["playlistId"] == "new-playlist-id"
    assert playlist_item_body["snippet"]["resourceId"]["videoId"] == "video123"
