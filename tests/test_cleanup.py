from __future__ import annotations

import os
from pathlib import Path

import pytest

from cleanup import cleanup_uploaded_recordings


def recording(directory: Path, stem: str, mtime: float) -> tuple[Path, Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / f"{stem}.mkv"
    srt = directory / f"{stem}_chat.srt"
    meta = directory / f"{stem}_meta.txt"
    video.write_bytes(b"video")
    srt.write_bytes(b"captions")
    meta.write_bytes(b"metadata")
    os.utime(video, (mtime, mtime))
    return video, srt, meta


def test_dry_run_returns_old_candidates_without_deleting(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    old = recording(tmp_path / "mrozopl" / "_uploaded", "old", now - 31 * 86400)
    recent = recording(tmp_path / "mrozopl" / "_uploaded", "recent", now - 2 * 86400)

    report = cleanup_uploaded_recordings(tmp_path, "_uploaded", 30, dry_run=True, now=now)

    assert set(report.candidates) == set(old)
    assert report.deleted == ()
    assert all(path.exists() for path in old + recent)
    assert report.total_bytes == sum(path.stat().st_size for path in old)


def test_real_cleanup_removes_video_and_companions(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    old = recording(tmp_path / "ctsg" / "_uploaded", "old", now - 40 * 86400)

    report = cleanup_uploaded_recordings(tmp_path, "_uploaded", 30, dry_run=False, now=now)

    assert set(report.deleted) == set(old)
    assert not any(path.exists() for path in old)


@pytest.mark.parametrize("invalid", ["..", "../outside", "folder/sub", "", "."])
def test_rejects_unsafe_uploaded_directory_name(tmp_path: Path, invalid: str) -> None:
    with pytest.raises(ValueError, match="single directory name"):
        cleanup_uploaded_recordings(tmp_path, invalid, 30)
