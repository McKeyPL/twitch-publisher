"""Single-pass, non-blocking scanning of Twitch recording directories."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator
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
    excluded_directory_names: Iterable[str] = (),
) -> Iterator[Path]:
    """Yield MKV files recursively while skipping uploaded directories."""
    root = Path(recordings_root)
    uploaded_name = uploaded_directory_name.strip().casefold()
    if not uploaded_name:
        raise ValueError("uploaded_directory_name cannot be empty")
    if not root.is_dir():
        return

    excluded_names = {
        name.strip().casefold()
        for name in excluded_directory_names
        if name.strip()
    }
    excluded_names.add(uploaded_name)
    candidates: list[Path] = []

    def report_walk_error(exc: OSError) -> None:
        logger.warning("Cannot scan recording directory: %s", exc)

    for directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=report_walk_error,
    ):
        # Pruning top-down is important: an _uploaded archive can contain many
        # terabytes and must never be traversed merely to discard its files later.
        directory_names[:] = sorted(
            (
                name
                for name in directory_names
                if name.casefold() not in excluded_names
            ),
            key=str.casefold,
        )
        current_directory = Path(directory)
        candidates.extend(
            current_directory / name
            for name in file_names
            if Path(name).suffix.casefold() == ".mkv"
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
    excluded_directory_names: Iterable[str] = (),
    expected_channel: str | None = None,
    now: float | None = None,
) -> list[ReadinessResult]:
    """Check every candidate once and include non-ready results."""
    root = Path(recordings_root)
    results: list[ReadinessResult] = []
    for video_path in iter_candidate_recordings(
        root,
        uploaded_directory_name,
        excluded_directory_names,
    ):
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
