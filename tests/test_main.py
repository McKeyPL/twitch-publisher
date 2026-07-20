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
        self.playlisted: list[str] = []

    @property
    def platform_name(self) -> str:
        return self.name

    def upload(self, video_path, title, description, tags, srt_path=None):
        self.uploaded.append(Path(video_path))
        if self.error:
            raise self.error
        return self.result

    def add_to_playlist(self, platform_video_id, playlist_identifier, *, playlist_title=None):
        self.playlisted.append(platform_video_id)
        return True


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
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nCzat", encoding="utf-8")
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
    video, metadata = make_recording(config.paths.recordings_root)
    uploaders = successful_uploaders(config)

    with StateStore(config.paths.database) as store:
        process_ready_recording(video, metadata, 13 * 3600, config, store, uploaders)
        assert store.get_status(video, "youtube").status is UploadStatus.SKIPPED
        assert store.get_status(video, "cda").status is UploadStatus.SUCCESS
        assert store.get_status(video, "rumble").status is UploadStatus.SUCCESS

    assert uploaders["youtube"].uploaded == []
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
        error_message="wynik publikacji nieznany",
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
