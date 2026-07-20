"""Recording readiness checks and MKV duration probing with ffprobe."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from meta_parser import MetadataError, StreamMetadata, meta_path_for_video, parse_meta_file


class DurationProbeError(RuntimeError):
    """ffprobe could not determine the file duration."""


class ReadinessStatus(str, Enum):
    FILE_MISSING = "file_missing"
    META_MISSING = "meta_missing"
    META_INVALID = "meta_invalid"
    ENDED_MISSING = "ended_missing"
    SIZE_CHECK_PENDING = "size_check_pending"
    SIZE_CHANGED = "size_changed"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    status: ReadinessStatus
    reason: str
    metadata: StreamMetadata | None = None

    @property
    def ready(self) -> bool:
        return self.status is ReadinessStatus.READY


@dataclass(slots=True)
class _SizeSample:
    size: int
    measured_at: float


class FileSizeStabilityTracker:
    """Keep samples between watcher cycles without calling sleep()."""

    def __init__(self, required_interval_seconds: float = 60.0) -> None:
        if required_interval_seconds <= 0:
            raise ValueError("required_interval_seconds must be greater than zero")
        self.required_interval_seconds = required_interval_seconds
        self._samples: dict[Path, _SizeSample] = {}

    def check(self, path: str | Path, *, now: float | None = None) -> ReadinessStatus:
        video_path = Path(path).resolve()
        measured_at = time.monotonic() if now is None else now
        size = video_path.stat().st_size
        previous = self._samples.get(video_path)

        if previous is None:
            self._samples[video_path] = _SizeSample(size=size, measured_at=measured_at)
            return ReadinessStatus.SIZE_CHECK_PENDING

        if measured_at - previous.measured_at < self.required_interval_seconds:
            return ReadinessStatus.SIZE_CHECK_PENDING

        if size != previous.size:
            self._samples[video_path] = _SizeSample(size=size, measured_at=measured_at)
            return ReadinessStatus.SIZE_CHANGED

        self._samples.pop(video_path, None)
        return ReadinessStatus.READY

    def forget(self, path: str | Path) -> None:
        self._samples.pop(Path(path).resolve(), None)


def check_recording_readiness(
    video_path: str | Path,
    tracker: FileSizeStabilityTracker,
    *,
    expected_channel: str | None = None,
    now: float | None = None,
) -> ReadinessResult:
    """Check the MKV, metadata, Ended field, and stable MKV size in order."""
    video = Path(video_path)
    if not video.is_file():
        tracker.forget(video)
        return ReadinessResult(ReadinessStatus.FILE_MISSING, f"MKV file is missing: {video}")

    meta_path = meta_path_for_video(video)
    if not meta_path.is_file():
        tracker.forget(video)
        return ReadinessResult(
            ReadinessStatus.META_MISSING,
            "_meta.txt is missing; the stream is probably still running",
        )

    try:
        metadata = parse_meta_file(meta_path, expected_channel=expected_channel)
    except MetadataError as exc:
        tracker.forget(video)
        return ReadinessResult(ReadinessStatus.META_INVALID, str(exc))

    if metadata.ended is None:
        tracker.forget(video)
        return ReadinessResult(
            ReadinessStatus.ENDED_MISSING,
            "The metadata file does not contain a populated Ended field",
            metadata,
        )

    status = tracker.check(video, now=now)
    reasons = {
        ReadinessStatus.SIZE_CHECK_PENDING: "Waiting for the second MKV size sample",
        ReadinessStatus.SIZE_CHANGED: "MKV size changed; muxing or flushing may still be in progress",
        ReadinessStatus.READY: "Metadata is complete and the MKV size is stable",
    }
    return ReadinessResult(status, reasons[status], metadata)


def probe_duration_seconds(
    video_path: str | Path,
    *,
    ffprobe_path: str = "ffprobe",
    timeout_seconds: float = 120.0,
) -> float:
    """Read duration from the container without parsing locale-dependent text."""
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(Path(video_path)),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DurationProbeError(f"ffprobe was not found: {ffprobe_path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DurationProbeError(f"ffprobe exceeded the {timeout_seconds} s timeout") from exc

    if completed.returncode != 0:
        details = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise DurationProbeError(f"ffprobe could not read the duration: {details}")

    try:
        duration = float(json.loads(completed.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DurationProbeError("ffprobe returned an invalid or empty duration") from exc

    if duration < 0:
        raise DurationProbeError("ffprobe returned a negative duration")
    return duration


def exceeds_duration_limit(duration_seconds: float, max_hours: float = 12.0) -> bool:
    """The boundary is strict: exactly 12 hours still qualifies for YouTube."""
    if duration_seconds < 0 or max_hours <= 0:
        raise ValueError("Invalid duration or limit")
    return duration_seconds > max_hours * 60 * 60
