"""Single-pass, non-blocking scanning of Twitch recording directories."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from duration_check import (
    FileSizeStabilityTracker,
    ReadinessResult,
    ReadinessStatus,
    check_recording_readiness,
)


logger = logging.getLogger(__name__)


def iter_candidate_recordings(
    recordings_root: Path,
    uploaded_directory_name: str,
) -> Iterator[Path]:
    """Yield MKV files recursively while skipping uploaded directories."""
    root = Path(recordings_root)
    uploaded_name = uploaded_directory_name.strip().casefold()
    if not uploaded_name:
        raise ValueError("uploaded_directory_name cannot be empty")
    if not root.is_dir():
        return

    candidates = (
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.casefold() == ".mkv"
        and uploaded_name
        not in {part.casefold() for part in path.relative_to(root).parts[:-1]}
    )
    yield from sorted(candidates, key=lambda path: str(path).casefold())


def _channel_for_video(video_path: Path, recordings_root: Path) -> str | None:
    relative = video_path.relative_to(recordings_root)
    return relative.parts[0] if len(relative.parts) > 1 else None


def scan_cycle(
    recordings_root: Path,
    tracker: FileSizeStabilityTracker,
    uploaded_directory_name: str,
    *,
    expected_channel: str | None = None,
    now: float | None = None,
) -> list[ReadinessResult]:
    """Check every candidate once and include non-ready results."""
    root = Path(recordings_root)
    results: list[ReadinessResult] = []
    for video_path in iter_candidate_recordings(root, uploaded_directory_name):
        channel = expected_channel
        if channel is None:
            channel = _channel_for_video(video_path, root)
        result = check_recording_readiness(
            video_path,
            tracker,
            expected_channel=channel,
            now=now,
        )
        results.append(result)
        if result.status is not ReadinessStatus.READY:
            logger.info("Recording is waiting: %s - %s", video_path, result.reason)
        else:
            logger.debug("Recording is ready: %s", video_path)
    return results
