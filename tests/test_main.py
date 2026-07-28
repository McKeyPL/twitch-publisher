from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import signal
import threading

import pytest

from config import Config, load_config
from duration_check import ReadinessResult, ReadinessStatus
from main import _request_stop, process_readiness_results, process_ready_recording
from media_splitter import MediaPart, SplitPlan
from meta_parser import StreamMetadata
from state import StateStore, UploadStatus
from uploaders.base import BaseUploader, UploadResult


class FakeUploader(BaseUploader):
    def __init__(
        self,
        name: str,
        retry_config,
        *,
        result: UploadResult | None = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__(retry_config)
        self.name = name
        self.result = result or UploadResult(True, f"{name}-id", f"https://{name}.test/id")
        self.error = error
        self.uploaded: list[Path] = []
        self.titles: list[str] = []
        self.playlisted: list[str] = []

    @property
    def platform_name(self) -> str:
        return self.name

    def upload(self, video_path, title, description, tags, srt_path=None):
        self.uploaded.append(Path(video_path))
        self.titles.append(title)
        if self.error:
            raise self.error
        return self.result

    def add_to_playlist(self, platform_video_id, playlist_identifier, *, playlist_title=None):
        self.playlisted.append(platform_video_id)
        return True


class CaptionRetryYouTubeUploader(FakeUploader):
    def __init__(self, retry_config) -> None:
        super().__init__(
            "youtube",
            retry_config,
            result=UploadResult(
                True,
                "youtube-id",
                "https://youtube.test/id",
                error_message="captions failed",
                captions_uploaded=False,
            ),
        )
        self.caption_attempts = 0

    def captions_required(self, srt_path: Path | None) -> bool:
        return bool(srt_path and Path(srt_path).stat().st_size)

    def upload_captions(self, platform_video_id: str, srt_path: Path):
        from uploaders.youtube import CaptionUploadResult

        self.caption_attempts += 1
        return CaptionUploadResult(True, caption_id="caption-id", status="serving")


class FakeMediaSplitter:
    def __init__(self) -> None:
        self.created_for: list[str] = []
        self.cleaned: list[str] = []

    def create_plan(
        self,
        source_path: Path,
        platform: str,
        duration_seconds: float,
        constraints,
        *,
        srt_path: Path | None = None,
    ) -> SplitPlan:
        self.created_for.append(platform)
        work = source_path.parent / "_publisher_work" / "fake" / platform
        work.mkdir(parents=True, exist_ok=True)
        boundary = duration_seconds / 2
        parts: list[MediaPart] = []
        for index, (start, end) in enumerate(
            ((0.0, boundary), (boundary, duration_seconds)),
            start=1,
        ):
            path = work / f"part_{index:03d}.mkv"
            path.write_bytes(f"part-{index}".encode())
            part_srt = work / f"part_{index:03d}_chat.srt"
            if srt_path is not None:
                part_srt.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nChat\n",
                    encoding="utf-8",
                )
            parts.append(
                MediaPart(
                    index=index,
                    path=path,
                    start_seconds=start,
                    end_seconds=end,
                    duration_seconds=end - start,
                    size_bytes=path.stat().st_size,
                    srt_path=part_srt if srt_path is not None else None,
                )
            )
        manifest = work / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        return SplitPlan(
            source_path=source_path,
            platform=platform,
            work_directory=work,
            manifest_path=manifest,
            segment_time_seconds=boundary,
            parts=tuple(parts),
        )

    def cleanup(self, plan: SplitPlan) -> None:
        self.cleaned.append(plan.platform)


class SequencedUploader(FakeUploader):
    def __init__(self, name: str, retry_config, results: list[UploadResult]) -> None:
        super().__init__(name, retry_config)
        self.results = list(results)

    def upload(self, video_path, title, description, tags, srt_path=None):
        self.uploaded.append(Path(video_path))
        self.titles.append(title)
        return self.results.pop(0)


def config_for(tmp_path: Path, monkeypatch) -> Config:
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRETS_FILE", "credentials.json")
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    paths = replace(
        config.paths,
        recordings_root=tmp_path / "recordings",
        database=tmp_path / "data" / "state.sqlite3",
        log_directory=tmp_path / "logs",
    )
    return replace(config, paths=paths)


def make_recording(root: Path, stem: str = "stream") -> tuple[Path, StreamMetadata]:
    directory = root / "mrozopl"
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / f"{stem}.mkv"
    video.write_bytes(b"video")
    srt = directory / f"{stem}_chat.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nChat", encoding="utf-8")
    meta_path = directory / f"{stem}_meta.txt"
    meta_path.write_text("metadata", encoding="utf-8")
    metadata = StreamMetadata(
        channel="mrozopl",
        title="Testowy stream !dss",
        game="Test Game",
        started=datetime(2026, 7, 12, 17, 0),
        ended=datetime(2026, 7, 12, 18, 0),
        quality="best",
        source_path=meta_path,
    )
    return video, metadata


def successful_uploaders(config: Config) -> dict[str, FakeUploader]:
    return {
        name: FakeUploader(name, config.retry)
        for name in ("youtube", "cda", "rumble")
    }


def test_end_to_end_success_moves_complete_recording(tmp_path: Path, monkeypatch) -> None:
    config = config_for(tmp_path, monkeypatch)
    video, metadata = make_recording(config.paths.recordings_root)
    uploaders = successful_uploaders(config)

    with StateStore(config.paths.database) as store:
        process_ready_recording(video, metadata, 3600, config, store, uploaders)
        assert all(
            store.get_status(video, name).status is UploadStatus.SUCCESS
            for name in uploaders
        )
        assert store.get_status(video, "youtube").playlist_added is True
        assert (
            store.get_status(video, "cda").platform_video_id
            == "https://cda.test/id"
        )
        assert (
            store.get_status(video, "rumble").platform_video_id
            == "https://rumble.test/id"
        )

    destination = video.parent / "_uploaded"
    assert (destination / video.name).is_file()
    assert (destination / "stream_chat.srt").is_file()
    assert (destination / "stream_meta.txt").is_file()


def test_one_failed_platform_does_not_block_others_or_move(tmp_path: Path, monkeypatch) -> None:
    config = config_for(tmp_path, monkeypatch)
    video, metadata = make_recording(config.paths.recordings_root)
    uploaders = successful_uploaders(config)
    uploaders["cda"].result = UploadResult(False, error_message="CDA failure")

    with StateStore(config.paths.database) as store:
        process_ready_recording(video, metadata, 3600, config, store, uploaders)
        assert store.get_status(video, "youtube").status is UploadStatus.SUCCESS
        assert store.get_status(video, "cda").status is UploadStatus.FAILED
        assert store.get_status(video, "rumble").status is UploadStatus.SUCCESS

    assert video.is_file()
    assert all(uploader.uploaded == [video] for uploader in uploaders.values())


def test_uploader_exception_does_not_block_next_platform_or_file(tmp_path: Path, monkeypatch) -> None:
    config = config_for(tmp_path, monkeypatch)
    first, first_meta = make_recording(config.paths.recordings_root, "first")
    second, second_meta = make_recording(config.paths.recordings_root, "second")
    uploaders = successful_uploaders(config)
    uploaders["youtube"].error = RuntimeError("boom")

    with StateStore(config.paths.database) as store:
        process_ready_recording(first, first_meta, 3600, config, store, uploaders)
        process_ready_recording(second, second_meta, 3600, config, store, uploaders)
        assert store.get_status(first, "youtube").status is UploadStatus.FAILED
        assert store.get_status(second, "youtube").status is UploadStatus.FAILED
        assert store.get_status(second, "cda").status is UploadStatus.SUCCESS

    assert uploaders["rumble"].uploaded == [first, second]


def test_invalid_readiness_result_does_not_block_ready_file(tmp_path: Path, monkeypatch) -> None:
    config = config_for(tmp_path, monkeypatch)
    video, metadata = make_recording(config.paths.recordings_root)
    uploaders = successful_uploaders(config)
    results = [
        ReadinessResult(ReadinessStatus.META_INVALID, "broken"),
        ReadinessResult(ReadinessStatus.READY, "ready", metadata),
    ]

    with StateStore(config.paths.database) as store:
        process_readiness_results(
            results,
            config,
            store,
            uploaders,
            duration_probe=lambda path: 3600,
        )
        assert store.get_status(video, "youtube").status is UploadStatus.SUCCESS

    assert not video.exists()


def test_duration_limit_skips_only_limited_platform(tmp_path: Path, monkeypatch) -> None:
    config = config_for(tmp_path, monkeypatch)
    config = replace(
        config,
        splitting=replace(config.splitting, enabled=False),
    )
    video, metadata = make_recording(config.paths.recordings_root)
    uploaders = successful_uploaders(config)

    with StateStore(config.paths.database) as store:
        process_ready_recording(video, metadata, 13 * 3600, config, store, uploaders)
        assert store.get_status(video, "youtube").status is UploadStatus.SKIPPED
        assert store.get_status(video, "cda").status is UploadStatus.SUCCESS
        assert store.get_status(video, "rumble").status is UploadStatus.SUCCESS

    assert uploaders["youtube"].uploaded == []
    assert not video.exists()


def test_long_youtube_recording_uploads_verified_parts_instead_of_skip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = config_for(tmp_path, monkeypatch)
    video, metadata = make_recording(config.paths.recordings_root)
    youtube = FakeUploader(
        "youtube",
        config.retry,
        result=UploadResult(
            True,
            "youtube-part-id",
            "https://youtube.test/part",
            captions_uploaded=True,
        ),
    )
    splitter = FakeMediaSplitter()

    with StateStore(config.paths.database) as store:
        process_ready_recording(
            video,
            metadata,
            13 * 3600,
            config,
            store,
            {"youtube": youtube},
            media_splitter=splitter,
        )
        parent = store.get_status(video, "youtube")
        parts = store.get_part_statuses(video, "youtube")

    assert parent is not None
    assert parent.status is UploadStatus.SUCCESS
    assert len(parts) == 2
    assert all(record.status is UploadStatus.SUCCESS for record in parts)
    assert len(youtube.uploaded) == 2
    assert youtube.titles[0].endswith("(Part 1/2)")
    assert youtube.titles[1].endswith("(Part 2/2)")
    assert all(len(title) <= 100 for title in youtube.titles)
    assert splitter.cleaned == ["youtube"]
    assert not video.exists()


def test_oversized_rumble_recording_uses_platform_specific_parts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = config_for(tmp_path, monkeypatch)
    rumble_config = replace(
        config.platforms.rumble,
        max_file_size_gb=0.00000001,
    )
    config = replace(
        config,
        platforms=replace(config.platforms, rumble=rumble_config),
        splitting=replace(config.splitting, rumble_target_size_gb=0.000000009),
    )
    video, metadata = make_recording(config.paths.recordings_root)
    video.write_bytes(b"x" * 20)
    rumble = FakeUploader("rumble", config.retry)
    splitter = FakeMediaSplitter()

    with StateStore(config.paths.database) as store:
        process_ready_recording(
            video,
            metadata,
            3600,
            config,
            store,
            {"rumble": rumble},
            media_splitter=splitter,
        )
        parent = store.get_status(video, "rumble")

    assert parent is not None
    assert parent.status is UploadStatus.SUCCESS
    assert len(rumble.uploaded) == 2
    assert all(len(title) <= 90 for title in rumble.titles)
    assert splitter.created_for == ["rumble"]
    assert splitter.cleaned == ["rumble"]


def test_multipart_restart_retries_only_failed_part(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = config_for(tmp_path, monkeypatch)
    config = replace(
        config,
        platforms=replace(
            config.platforms,
            rumble=replace(
                config.platforms.rumble,
                max_file_size_gb=0.00000001,
            ),
        ),
        splitting=replace(config.splitting, rumble_target_size_gb=0.000000009),
    )
    video, metadata = make_recording(config.paths.recordings_root)
    video.write_bytes(b"x" * 20)
    rumble = SequencedUploader(
        "rumble",
        config.retry,
        [
            UploadResult(True, "part-1", "https://rumble.test/part-1"),
            UploadResult(False, error_message="temporary failure"),
            UploadResult(True, "part-2", "https://rumble.test/part-2"),
        ],
    )
    splitter = FakeMediaSplitter()

    with StateStore(config.paths.database) as store:
        process_ready_recording(
            video,
            metadata,
            3600,
            config,
            store,
            {"rumble": rumble},
            media_splitter=splitter,
        )
        first_cycle = store.get_part_statuses(video, "rumble")
        assert [record.status for record in first_cycle] == [
            UploadStatus.SUCCESS,
            UploadStatus.FAILED,
        ]
        assert video.is_file()

        process_ready_recording(
            video,
            metadata,
            3600,
            config,
            store,
            {"rumble": rumble},
            media_splitter=splitter,
        )
        second_cycle = store.get_part_statuses(video, "rumble")
        assert [record.status for record in second_cycle] == [
            UploadStatus.SUCCESS,
            UploadStatus.SUCCESS,
        ]

    assert [path.name for path in rumble.uploaded] == [
        "part_001.mkv",
        "part_002.mkv",
        "part_002.mkv",
    ]
    assert not video.exists()


def test_successful_video_retries_only_missing_captions_on_next_cycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = config_for(tmp_path, monkeypatch)
    video, metadata = make_recording(config.paths.recordings_root)
    youtube = CaptionRetryYouTubeUploader(config.retry)

    with StateStore(config.paths.database) as store:
        process_ready_recording(
            video,
            metadata,
            3600,
            config,
            store,
            {"youtube": youtube},
        )
        first = store.get_status(video, "youtube")
        assert first is not None
        assert first.status is UploadStatus.SUCCESS
        assert first.captions_uploaded is False
        assert video.is_file()

        process_ready_recording(
            video,
            metadata,
            3600,
            config,
            store,
            {"youtube": youtube},
        )
        second = store.get_status(video, "youtube")
        assert second is not None
        assert second.captions_uploaded is True

    assert youtube.uploaded == [video]
    assert youtube.caption_attempts == 1
    assert not video.exists()


def test_rumble_title_is_limited_to_90_characters(tmp_path: Path, monkeypatch) -> None:
    config = config_for(tmp_path, monkeypatch)
    video, metadata = make_recording(config.paths.recordings_root)
    metadata = replace(metadata, title="Very long stream title " * 20)
    rumble = FakeUploader("rumble", config.retry)

    with StateStore(config.paths.database) as store:
        process_ready_recording(
            video,
            metadata,
            3600,
            config,
            store,
            {"rumble": rumble},
        )

    assert len(rumble.titles) == 1
    assert len(rumble.titles[0]) == 90
    assert rumble.titles[0].endswith(" | mrozopl | 2026-07-12")


def test_cda_normalizes_recording_filename_before_upload(
    tmp_path: Path, monkeypatch
) -> None:
    config = config_for(tmp_path, monkeypatch)
    stem = "20260714_170854_mrozopl_Arduino 📻 !dss"
    video, metadata = make_recording(config.paths.recordings_root, stem)
    cda = FakeUploader("cda", config.retry)

    with StateStore(config.paths.database) as store:
        process_ready_recording(
            video,
            metadata,
            3600,
            config,
            store,
            {"cda": cda},
        )

    normalized_name = "20260714_170854_mrozopl_Arduino.mkv"
    normalized_path = video.with_name(normalized_name)
    assert cda.uploaded == [normalized_path]
    assert not video.exists()
    destination = (
        config.paths.recordings_root
        / "mrozopl"
        / config.moving.uploaded_directory_name
    )
    assert (destination / normalized_name).is_file()
    assert (
        destination / "20260714_170854_mrozopl_Arduino_chat.srt"
    ).is_file()
    assert (
        destination / "20260714_170854_mrozopl_Arduino_meta.txt"
    ).is_file()


def test_rumble_no_video_track_skip_allows_recording_move(
    tmp_path: Path, monkeypatch
) -> None:
    config = config_for(tmp_path, monkeypatch)
    video, metadata = make_recording(config.paths.recordings_root)
    rumble = FakeUploader(
        "rumble",
        config.retry,
        result=UploadResult(
            success=False,
            error_message="Rumble skipped the recording because it has no video track",
            retry_allowed=False,
            skipped=True,
        ),
    )

    with StateStore(config.paths.database) as store:
        process_ready_recording(
            video,
            metadata,
            3600,
            config,
            store,
            {"rumble": rumble},
        )
        record = store.get_status(video, "rumble")

    assert record is not None
    assert record.status is UploadStatus.SKIPPED
    assert "no video track" in (record.last_error or "")
    assert not video.exists()
    assert (
        config.paths.recordings_root
        / "mrozopl"
        / config.moving.uploaded_directory_name
        / video.name
    ).is_file()


def test_existing_rumble_no_video_track_failure_is_migrated_to_skip(
    tmp_path: Path, monkeypatch
) -> None:
    config = config_for(tmp_path, monkeypatch)
    video, metadata = make_recording(config.paths.recordings_root)
    rumble = FakeUploader("rumble", config.retry)

    with StateStore(config.paths.database) as store:
        store.mark_failed(
            video,
            "rumble",
            "[NO_AUTO_RETRY] The video file has no video track",
        )
        process_ready_recording(
            video,
            metadata,
            3600,
            config,
            store,
            {"rumble": rumble},
        )
        record = store.get_status(video, "rumble")

    assert record is not None
    assert record.status is UploadStatus.SKIPPED
    assert rumble.uploaded == []
    assert not video.exists()


def test_existing_rumble_file_size_failure_is_migrated_to_skip(
    tmp_path: Path, monkeypatch
) -> None:
    config = config_for(tmp_path, monkeypatch)
    video, metadata = make_recording(config.paths.recordings_root)
    rumble = FakeUploader("rumble", config.retry)

    with StateStore(config.paths.database) as store:
        store.mark_failed(
            video,
            "rumble",
            "[NO_AUTO_RETRY] File exceeds the Rumble 15 GB limit: recording.mkv",
        )
        process_ready_recording(
            video,
            metadata,
            3600,
            config,
            store,
            {"rumble": rumble},
        )
        record = store.get_status(video, "rumble")

    assert record is not None
    assert record.status is UploadStatus.SKIPPED
    assert rumble.uploaded == []
    assert not video.exists()


def test_sigint_interrupts_active_operation_immediately() -> None:
    event = threading.Event()

    with pytest.raises(KeyboardInterrupt):
        _request_stop(event, signal.SIGINT, None)

    assert event.is_set() is True


def test_unknown_browser_outcome_is_not_retried_next_cycle(tmp_path: Path, monkeypatch) -> None:
    config = config_for(tmp_path, monkeypatch)
    video, metadata = make_recording(config.paths.recordings_root)
    uploaders = successful_uploaders(config)
    uploaders["cda"].result = UploadResult(
        False,
        error_message="publication result unknown",
        retry_allowed=False,
    )

    with StateStore(config.paths.database) as store:
        process_ready_recording(video, metadata, 3600, config, store, uploaders)
        process_ready_recording(video, metadata, 3600, config, store, uploaders)
        record = store.get_status(video, "cda")

    assert uploaders["cda"].uploaded == [video]
    assert record is not None
    assert record.status is UploadStatus.FAILED
    assert (record.last_error or "").startswith("[NO_AUTO_RETRY]")
