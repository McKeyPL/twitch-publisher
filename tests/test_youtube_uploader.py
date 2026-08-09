from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from config import RetryConfig, YouTubeConfig
from state import StateStore
from uploaders.youtube import SCOPES, YouTubeUploader, _pacific_quota_window


@pytest.fixture
def youtube_config(tmp_path: Path) -> YouTubeConfig:
    return YouTubeConfig(
        enabled=True,
        client_secrets_file=tmp_path / "credentials.json",
        token_file=tmp_path / "auth" / "token.json",
        privacy_status="unlisted",
        max_duration_hours=12,
        max_file_size_gb=256,
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
            patch.object(
                uploader.api_client, "get_credentials", return_value=MagicMock()
            ) as credentials,
            patch("youtube_api.build", return_value=service) as mocked_build,
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


def _cached_credentials(*, valid: bool = True, expired: bool = False) -> MagicMock:
    credentials = MagicMock()
    credentials.valid = valid
    credentials.expired = expired
    credentials.refresh_token = "refresh-token"
    credentials.has_scopes.return_value = True
    credentials.to_json.return_value = '{"token":"saved"}'
    return credentials


def test_uses_valid_cached_oauth_token_without_interactive_flow(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    youtube_config.token_file.parent.mkdir(parents=True)
    youtube_config.token_file.write_text("{}", encoding="utf-8")
    credentials = _cached_credentials()

    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        with (
            patch(
                "youtube_api.Credentials.from_authorized_user_file",
                return_value=credentials,
            ) as load_token,
            patch(
                "youtube_api.InstalledAppFlow.from_client_secrets_file"
            ) as flow,
        ):
            assert uploader._get_credentials() is credentials

    load_token.assert_called_once_with(youtube_config.token_file, uploader_module_scopes())
    flow.assert_not_called()
    assert youtube_config.token_file.read_text(encoding="utf-8") == '{"token":"saved"}'


def uploader_module_scopes() -> list[str]:
    return SCOPES


def test_oauth_scopes_cover_upload_captions_and_playlists() -> None:
    assert SCOPES == [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    ]


def test_refreshes_expired_token_and_persists_it(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    youtube_config.token_file.parent.mkdir(parents=True)
    youtube_config.token_file.write_text("{}", encoding="utf-8")
    credentials = _cached_credentials(valid=False, expired=True)

    def refreshed(_request: object) -> None:
        credentials.expired = False
        credentials.valid = True

    credentials.refresh.side_effect = refreshed
    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        with (
            patch(
                "youtube_api.Credentials.from_authorized_user_file",
                return_value=credentials,
            ),
            patch("youtube_api.Request", return_value=MagicMock()) as request,
            patch(
                "youtube_api.InstalledAppFlow.from_client_secrets_file"
            ) as flow,
        ):
            assert uploader._get_credentials() is credentials

    credentials.refresh.assert_called_once_with(request.return_value)
    flow.assert_not_called()
    assert youtube_config.token_file.read_text(encoding="utf-8") == '{"token":"saved"}'


@pytest.mark.parametrize("failure_mode", ["refresh_error", "missing_scope"])
def test_falls_back_to_interactive_oauth_when_cached_token_cannot_be_used(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
    failure_mode: str,
) -> None:
    youtube_config.token_file.parent.mkdir(parents=True)
    youtube_config.token_file.write_text("{}", encoding="utf-8")
    credentials = _cached_credentials(
        valid=failure_mode == "missing_scope",
        expired=failure_mode == "refresh_error",
    )
    if failure_mode == "refresh_error":
        credentials.refresh.side_effect = RefreshError("refresh failed")
    else:
        credentials.has_scopes.return_value = False
    authorized = _cached_credentials()
    flow = MagicMock()
    flow.run_local_server.return_value = authorized

    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        with (
            patch(
                "youtube_api.Credentials.from_authorized_user_file",
                return_value=credentials,
            ),
            patch(
                "youtube_api.InstalledAppFlow.from_client_secrets_file",
                return_value=flow,
            ) as create_flow,
        ):
            assert uploader._get_credentials() is authorized

    create_flow.assert_called_once_with(
        str(youtube_config.client_secrets_file),
        uploader_module_scopes(),
    )
    flow.run_local_server.assert_called_once_with(port=0, open_browser=True)
    assert youtube_config.token_file.read_text(encoding="utf-8") == '{"token":"saved"}'


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
            patch("youtube_api.build") as mocked_build,
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


def test_retries_only_captions_for_an_existing_video_and_reserves_quota(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    srt = tmp_path / "stream_chat.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nChat\n", encoding="utf-8")
    service = MagicMock()
    request = MagicMock()
    request.execute.return_value = {
        "id": "caption-id",
        "snippet": {"status": "serving"},
    }
    service.captions.return_value.insert.return_value = request

    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        uploader._service = service
        with patch("uploaders.youtube.MediaFileUpload") as media_upload:
            result = uploader.upload_captions("existing-video", srt)

        period, _ = _pacific_quota_window()
        assert store.get_quota_usage("youtube_general", period) == 400
        assert store.get_quota_usage("youtube_videos_insert", period) == 0

    assert result.success is True
    assert result.caption_id == "caption-id"
    assert result.status == "serving"
    media_upload.assert_called_once_with(
        str(srt),
        mimetype="application/octet-stream",
        resumable=False,
    )


def test_caption_response_with_failed_status_is_not_marked_as_uploaded(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    srt = tmp_path / "stream_chat.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nChat\n", encoding="utf-8")
    service = MagicMock()
    request = MagicMock()
    request.execute.return_value = {
        "id": "caption-id",
        "snippet": {"status": "failed", "failureReason": "processingFailed"},
    }
    service.captions.return_value.insert.return_value = request

    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        uploader._service = service
        with patch("uploaders.youtube.MediaFileUpload"):
            result = uploader.upload_captions("existing-video", srt)

    assert result.success is False
    assert result.status == "failed"
    assert "processingFailed" in (result.error_message or "")


def test_caption_exists_conflict_is_idempotent_success(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    srt = tmp_path / "stream_chat.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nChat\n", encoding="utf-8")
    conflict = HttpError(
        httplib2.Response({"status": "409"}),
        b'{"error":{"errors":[{"reason":"captionExists"}]}}',
    )
    service = MagicMock()
    request = MagicMock()
    request.execute.side_effect = conflict
    service.captions.return_value.insert.return_value = request

    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        uploader._service = service
        with patch("uploaders.youtube.MediaFileUpload"):
            result = uploader.upload_captions("existing-video", srt)

    assert result.success is True
    assert result.status == "existing"


def test_invalid_utf8_srt_is_not_eligible_for_upload(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    srt = tmp_path / "invalid.srt"
    srt.write_bytes(b"\xff\xfe\x00\x01")

    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        assert uploader.captions_required(srt) is False


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
        period, _ = _pacific_quota_window()
        assert store.get_quota_usage("youtube_general", period) == 100

    assert added is True
    assert "YT_PLAYLIST_MROZOPL=new-playlist-id" in caplog.text
    playlist_item_body = service.playlistItems.return_value.insert.call_args.kwargs["body"]
    assert playlist_item_body["snippet"]["playlistId"] == "new-playlist-id"
    assert playlist_item_body["snippet"]["resourceId"]["videoId"] == "video123"


def test_playlist_lookup_and_insert_reserve_general_quota(
    tmp_path: Path,
    youtube_config: YouTubeConfig,
    retry_config: RetryConfig,
) -> None:
    service = MagicMock()
    lookup = MagicMock()
    lookup.execute.return_value = {"items": [{"id": "playlist-id"}]}
    service.playlists.return_value.list.return_value = lookup
    insert = MagicMock()
    insert.execute.return_value = {"id": "playlist-item"}
    service.playlistItems.return_value.insert.return_value = insert

    with StateStore(tmp_path / "state.sqlite3") as store:
        uploader = YouTubeUploader(youtube_config, retry_config, store)
        uploader._service = service
        assert uploader.add_to_playlist("video-id", "playlist-id") is True
        period, _ = _pacific_quota_window()
        assert store.get_quota_usage("youtube_general", period) == 51
