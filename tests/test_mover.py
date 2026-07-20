from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

import mover
from config import Config, load_config
from mover import move_processed_recording
from state import Platform, StateStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PLATFORMS = ["youtube", "cda", "rumble"]


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRETS_FILE", "auth/credentials.json")
    return load_config(PROJECT_ROOT / "config.yaml")


def mark_fully_processed(store: StateStore, video: Path) -> None:
    for platform in Platform:
        store.mark_success(video, platform, f"{platform.value}-id")


def create_recording_triplet(directory: Path, *, empty_srt: bool = False) -> tuple[Path, Path, Path]:
    video = directory / "20260712_172422_mrozopl_test.mkv"
    srt = directory / "20260712_172422_mrozopl_test_chat.srt"
    meta = directory / "20260712_172422_mrozopl_test_meta.txt"
    video.write_bytes(b"video")
    srt.write_bytes(b"" if empty_srt else b"1\n00:00:01 --> 00:00:02\nchat\n")
    meta.write_text("Channel : mrozopl\nEnded : 2026-07-12T20:00:00\n", encoding="utf-8")
    return video, srt, meta


def test_moves_video_srt_and_meta(tmp_path: Path, config: Config) -> None:
    streamer = tmp_path / "mrozopl"
    streamer.mkdir()
    video, srt, meta = create_recording_triplet(streamer)

    with StateStore(tmp_path / "state.sqlite3") as store:
        mark_fully_processed(store, video)
        result = move_processed_recording(video, config, store, REQUIRED_PLATFORMS)

    destination = streamer / "_uploaded"
    assert result.moved is True
    assert result.already_done is False
    assert result.destination == destination / video.name
    assert result.warnings == []
    assert not video.exists() and not srt.exists() and not meta.exists()
    assert (destination / video.name).read_bytes() == b"video"
    assert (destination / srt.name).is_file()
    assert (destination / meta.name).is_file()


def test_moves_zero_byte_srt_and_logs_info(
    tmp_path: Path,
    config: Config,
    caplog: pytest.LogCaptureFixture,
) -> None:
    streamer = tmp_path / "jalowy_gracz"
    streamer.mkdir()
    video, srt, _ = create_recording_triplet(streamer, empty_srt=True)

    with StateStore(tmp_path / "state.sqlite3") as store, caplog.at_level(logging.INFO):
        mark_fully_processed(store, video)
        result = move_processed_recording(video, config, store, REQUIRED_PLATFORMS)

    moved_srt = streamer / "_uploaded" / srt.name
    assert result.moved is True
    assert moved_srt.is_file() and moved_srt.stat().st_size == 0
    assert "empty SRT captions file" in caplog.text


def test_does_nothing_when_not_fully_processed(tmp_path: Path, config: Config) -> None:
    streamer = tmp_path / "mrozopl"
    streamer.mkdir()
    video, srt, meta = create_recording_triplet(streamer)

    with StateStore(tmp_path / "state.sqlite3") as store:
        store.mark_success(video, Platform.YOUTUBE, "yt-id")
        store.mark_failed(video, Platform.CDA, "timeout")
        result = move_processed_recording(video, config, store, REQUIRED_PLATFORMS)

    assert result.moved is False
    assert "Not all required platforms" in (result.reason or "")
    assert video.exists() and srt.exists() and meta.exists()
    assert not (streamer / "_uploaded").exists()


def test_detects_video_already_moved_and_finishes_companions(
    tmp_path: Path,
    config: Config,
) -> None:
    streamer = tmp_path / "mrozopl"
    destination = streamer / "_uploaded"
    destination.mkdir(parents=True)
    video = streamer / "stream.mkv"
    srt = streamer / "stream_chat.srt"
    meta = streamer / "stream_meta.txt"
    srt.write_text("chat", encoding="utf-8")
    meta.write_text("meta", encoding="utf-8")
    (destination / video.name).write_bytes(b"already moved")

    with StateStore(tmp_path / "state.sqlite3") as store:
        mark_fully_processed(store, video)
        result = move_processed_recording(video, config, store, REQUIRED_PLATFORMS)

    assert result.moved is True and result.already_done is True
    assert result.destination == destination / video.name
    assert (destination / srt.name).is_file()
    assert (destination / meta.name).is_file()
    assert not srt.exists() and not meta.exists()


def test_race_condition_video_disappears_before_shutil_move(
    tmp_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamer = tmp_path / "mrozopl"
    streamer.mkdir()
    video, _, _ = create_recording_triplet(streamer)

    def disappear(source: str, destination: str) -> None:
        Path(source).unlink()
        raise FileNotFoundError(source)

    monkeypatch.setattr(mover.shutil, "move", disappear)
    with StateStore(tmp_path / "state.sqlite3") as store:
        mark_fully_processed(store, video)
        result = move_processed_recording(video, config, store, REQUIRED_PLATFORMS)

    assert result.moved is False
    assert "Could not move the main MKV file" in (result.reason or "")


def test_companion_failure_does_not_roll_back_video_or_stop_meta(
    tmp_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamer = tmp_path / "mrozopl"
    streamer.mkdir()
    video, srt, meta = create_recording_triplet(streamer)
    real_move = shutil.move

    def fail_for_srt(source: str, destination: str) -> str:
        if Path(source).suffix.casefold() == ".srt":
            raise PermissionError("SRT is locked")
        return real_move(source, destination)

    monkeypatch.setattr(mover.shutil, "move", fail_for_srt)
    with StateStore(tmp_path / "state.sqlite3") as store:
        mark_fully_processed(store, video)
        result = move_processed_recording(video, config, store, REQUIRED_PLATFORMS)

    destination = streamer / "_uploaded"
    assert result.moved is True
    assert len(result.warnings) == 1
    assert "manual intervention" in result.warnings[0]
    assert (destination / video.name).is_file()
    assert srt.is_file()
    assert (destination / meta.name).is_file()
    assert not meta.exists()
