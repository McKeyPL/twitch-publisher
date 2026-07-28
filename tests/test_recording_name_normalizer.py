from __future__ import annotations

from pathlib import Path

import pytest

from recording_name_normalizer import (
    RecordingNameNormalizationError,
    normalize_recording_set_for_cda,
    sanitize_cda_filename_stem,
)
from state import Platform, StateStore, UploadStatus


def test_sanitizer_keeps_bmp_unicode_and_removes_emoji_and_commands() -> None:
    raw = (
        "20260714_170854_mrozopl_[Daj Sobie Szansę] "
        "Arduino 📻 ⚙️ 日本語 !dss"
    )

    assert sanitize_cda_filename_stem(raw) == (
        "20260714_170854_mrozopl_[Daj Sobie Szansę] Arduino 日本語"
    )


def test_sanitizer_repairs_mojibake_and_removes_unsafe_codepoints() -> None:
    mojibake = "Szansę".encode("utf-8").decode("cp1252")
    raw = f"{mojibake} \ue000 \x01 🧪"

    assert sanitize_cda_filename_stem(raw) == "Szansę"


def test_normalizer_renames_complete_set_and_migrates_state(tmp_path: Path) -> None:
    directory = tmp_path / "mrozopl"
    directory.mkdir()
    stem = "20260714_170854_mrozopl_Arduino 📻 !dss"
    video = directory / f"{stem}.mkv"
    srt = directory / f"{stem}_chat.srt"
    metadata = directory / f"{stem}_meta.txt"
    video.write_bytes(b"video")
    srt.write_bytes(b"")
    metadata.write_text("metadata", encoding="utf-8")

    with StateStore(tmp_path / "state.sqlite3") as store:
        store.mark_success(video, Platform.YOUTUBE, "yt-id")
        store.mark_failed(video, Platform.CDA, "HTTP 500")

        result = normalize_recording_set_for_cda(video, store)

        assert result.renamed is True
        assert result.migrated_statuses == 2
        assert result.video_path.name == "20260714_170854_mrozopl_Arduino.mkv"
        assert result.video_path.is_file()
        assert result.srt_path is not None and result.srt_path.is_file()
        assert result.srt_path.stat().st_size == 0
        assert result.metadata_path.is_file()
        assert not video.exists()
        assert store.get_status(video, Platform.YOUTUBE) is None
        assert (
            store.get_status(result.video_path, Platform.YOUTUBE).status
            is UploadStatus.SUCCESS
        )
        assert (
            store.get_status(result.video_path, Platform.CDA).status
            is UploadStatus.FAILED
        )


def test_normalizer_refuses_to_overwrite_existing_target(tmp_path: Path) -> None:
    stem = "Stream 📻"
    video = tmp_path / f"{stem}.mkv"
    metadata = tmp_path / f"{stem}_meta.txt"
    target = tmp_path / "Stream.mkv"
    video.write_bytes(b"source")
    metadata.write_text("metadata", encoding="utf-8")
    target.write_bytes(b"target")

    with StateStore(tmp_path / "state.sqlite3") as store:
        with pytest.raises(RecordingNameNormalizationError, match="overwrite"):
            normalize_recording_set_for_cda(video, store)

    assert video.read_bytes() == b"source"
    assert metadata.is_file()
    assert target.read_bytes() == b"target"


def test_normalizer_rolls_files_back_when_sqlite_target_conflicts(
    tmp_path: Path,
) -> None:
    video = tmp_path / "Stream 📻.mkv"
    metadata = tmp_path / "Stream 📻_meta.txt"
    target = tmp_path / "Stream.mkv"
    video.write_bytes(b"video")
    metadata.write_text("metadata", encoding="utf-8")

    with StateStore(tmp_path / "state.sqlite3") as store:
        store.mark_failed(video, Platform.CDA, "old path")
        store.mark_failed(target, Platform.RUMBLE, "target path")

        with pytest.raises(
            RecordingNameNormalizationError,
            match="rolled back",
        ):
            normalize_recording_set_for_cda(video, store)

        assert store.get_status(video, Platform.CDA) is not None
        assert store.get_status(target, Platform.RUMBLE) is not None

    assert video.read_bytes() == b"video"
    assert metadata.is_file()
    assert not target.exists()
