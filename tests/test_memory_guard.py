from __future__ import annotations

from pathlib import Path

import pytest

from config import MemoryConfig, RetryConfig
from memory_guard import GIB, MemoryGuard, MemoryPressureError, MemorySnapshot
from uploaders.base import BaseUploader, UploadResult


def memory_config(**overrides) -> MemoryConfig:
    values = {
        "enabled": True,
        "minimum_commit_headroom_gb": 16,
        "minimum_physical_available_gb": 4,
        "check_interval_seconds": 5,
        "youtube_reserve_mb": 256,
        "browser_reserve_mb": 2048,
        "split_reserve_mb": 1024,
    }
    values.update(overrides)
    return MemoryConfig(**values)


def test_allows_operation_with_commit_and_physical_headroom() -> None:
    snapshot = MemorySnapshot(100 * GIB, 140 * GIB, 20 * GIB, "test")
    guard = MemoryGuard(memory_config(), snapshot_provider=lambda: snapshot)

    assert guard.ensure_safe("upload", reserve_bytes=2 * GIB) == snapshot


def test_blocks_when_commit_headroom_cannot_cover_floor_and_reserve() -> None:
    snapshot = MemorySnapshot(125 * GIB, 140 * GIB, 20 * GIB, "test")
    guard = MemoryGuard(memory_config(), snapshot_provider=lambda: snapshot)

    with pytest.raises(MemoryPressureError, match="commit headroom.*17.00 GiB"):
        guard.ensure_safe("youtube upload", reserve_bytes=GIB)


def test_blocks_on_low_physical_memory_even_when_commit_is_available() -> None:
    snapshot = MemorySnapshot(50 * GIB, 140 * GIB, 3 * GIB, "test")
    guard = MemoryGuard(memory_config(), snapshot_provider=lambda: snapshot)

    with pytest.raises(MemoryPressureError, match="physical available"):
        guard.ensure_safe("browser upload")


def test_snapshot_reads_are_rate_limited() -> None:
    calls = 0
    clock = 10.0

    def provider() -> MemorySnapshot:
        nonlocal calls
        calls += 1
        return MemorySnapshot(50 * GIB, 140 * GIB, 20 * GIB, "test")

    guard = MemoryGuard(
        memory_config(),
        snapshot_provider=provider,
        monotonic=lambda: clock,
    )
    guard.ensure_safe("first")
    guard.ensure_safe("second")

    assert calls == 1


class FailingUploader(BaseUploader):
    @property
    def platform_name(self) -> str:
        return "test"

    def upload(self, video_path, title, description, tags, srt_path=None):
        return UploadResult(True)

    def add_to_playlist(
        self,
        platform_video_id,
        playlist_identifier,
        *,
        playlist_title=None,
    ):
        return False


@pytest.mark.parametrize("error", [MemoryError("oom"), MemoryPressureError("guard")])
def test_retry_never_repeats_memory_failures(error: Exception) -> None:
    attempts = 0
    uploader = FailingUploader(RetryConfig(3, 0.001, 2, 0.01))

    def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise error

    with pytest.raises(type(error)):
        uploader._with_retry(operation, operation_name="memory test")

    assert attempts == 1
