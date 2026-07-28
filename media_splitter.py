"""Lossless, resumable FFmpeg segmentation for platform upload limits."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from duration_check import probe_duration_seconds
from srt_splitter import SRTError, SegmentWindow, split_srt_file


logger = logging.getLogger(__name__)
MANIFEST_VERSION = 1


class MediaSplitError(RuntimeError):
    """Raised when a source cannot be split into verified safe parts."""


@dataclass(frozen=True, slots=True)
class SplitConstraints:
    hard_max_duration_seconds: float | None = None
    target_duration_seconds: float | None = None
    hard_max_size_bytes: int | None = None
    target_size_bytes: int | None = None

    def __post_init__(self) -> None:
        numeric = {
            "hard_max_duration_seconds": self.hard_max_duration_seconds,
            "target_duration_seconds": self.target_duration_seconds,
            "hard_max_size_bytes": self.hard_max_size_bytes,
            "target_size_bytes": self.target_size_bytes,
        }
        if not any(value is not None for value in numeric.values()):
            raise ValueError("At least one split constraint is required")
        for name, value in numeric.items():
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if (
            self.hard_max_duration_seconds is not None
            and self.target_duration_seconds is not None
            and self.target_duration_seconds > self.hard_max_duration_seconds
        ):
            raise ValueError("Target duration cannot exceed hard duration")
        if (
            self.hard_max_size_bytes is not None
            and self.target_size_bytes is not None
            and self.target_size_bytes > self.hard_max_size_bytes
        ):
            raise ValueError("Target size cannot exceed hard size")


@dataclass(frozen=True, slots=True)
class MediaPart:
    index: int
    path: Path
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    size_bytes: int
    srt_path: Path | None = None


@dataclass(frozen=True, slots=True)
class SplitPlan:
    source_path: Path
    platform: str
    work_directory: Path
    manifest_path: Path
    segment_time_seconds: float
    parts: tuple[MediaPart, ...]
    reused: bool = False


def _source_identity(source: Path) -> dict[str, Any]:
    stat = source.stat()
    return {
        "path": os.path.normcase(str(source.resolve(strict=False))),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _constraints_dict(constraints: SplitConstraints) -> dict[str, Any]:
    return asdict(constraints)


class MediaSplitter:
    def __init__(
        self,
        *,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        work_directory_name: str = "_publisher_work",
        max_replans: int = 3,
        disk_space_multiplier: float = 1.05,
        cancel_event: threading.Event | None = None,
        duration_probe: Callable[[Path], float] | None = None,
    ) -> None:
        work_name = work_directory_name.strip()
        if (
            not work_name
            or Path(work_name).is_absolute()
            or len(Path(work_name).parts) != 1
            or work_name in {".", ".."}
        ):
            raise ValueError("work_directory_name must be one safe directory name")
        if max_replans < 0:
            raise ValueError("max_replans must be >= 0")
        if disk_space_multiplier <= 1:
            raise ValueError("disk_space_multiplier must be greater than 1")
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.work_directory_name = work_name
        self.max_replans = max_replans
        self.disk_space_multiplier = disk_space_multiplier
        self.cancel_event = cancel_event
        self.duration_probe = duration_probe or (
            lambda path: probe_duration_seconds(
                path,
                ffprobe_path=self.ffprobe_path,
            )
        )

    def _raise_if_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise KeyboardInterrupt("Media splitting was interrupted")

    def _work_directory(self, source: Path, platform: str) -> Path:
        identity = _source_identity(source)
        digest_input = (
            f"{identity['path']}\0{identity['size']}\0{identity['mtime_ns']}"
        ).encode("utf-8", errors="surrogatepass")
        digest = hashlib.sha256(digest_input).hexdigest()[:16]
        safe_platform = "".join(
            character for character in platform.casefold() if character.isalnum()
        )
        if not safe_platform:
            raise ValueError("platform must contain at least one alphanumeric character")
        return (
            source.parent
            / self.work_directory_name
            / digest
            / safe_platform
        )

    def _initial_segment_time(
        self,
        source: Path,
        duration_seconds: float,
        constraints: SplitConstraints,
    ) -> float:
        candidates: list[float] = []
        duration_target = (
            constraints.target_duration_seconds
            or constraints.hard_max_duration_seconds
        )
        if duration_target is not None:
            candidates.append(float(duration_target))
        size_target = constraints.target_size_bytes or constraints.hard_max_size_bytes
        if size_target is not None:
            source_size = source.stat().st_size
            if source_size <= 0:
                raise MediaSplitError(f"Source video is empty: {source}")
            candidates.append(duration_seconds * size_target / source_size)
        segment_time = min(candidates)
        if segment_time <= 0:
            raise MediaSplitError("Calculated FFmpeg segment time is not positive")
        return min(segment_time, duration_seconds)

    def _check_disk_space(self, source: Path, work_directory: Path) -> None:
        work_directory.parent.mkdir(parents=True, exist_ok=True)
        required = round(source.stat().st_size * self.disk_space_multiplier)
        free = shutil.disk_usage(work_directory.parent).free
        if free < required:
            raise MediaSplitError(
                f"Not enough free disk space for lossless split: need about "
                f"{required} bytes, available {free} bytes at {work_directory.parent}"
            )

    def _run_ffmpeg(
        self,
        source: Path,
        work_directory: Path,
        segment_time_seconds: float,
    ) -> None:
        self._raise_if_cancelled()
        list_path = work_directory / "parts.csv"
        pattern = work_directory / "part_%03d.mkv"
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map",
            "0",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            f"{segment_time_seconds:.6f}",
            "-reset_timestamps",
            "1",
            "-segment_list",
            str(list_path),
            "-segment_list_type",
            "csv",
            str(pattern),
        ]
        logger.info(
            "Splitting %s losslessly with target segment time %.3f s",
            source,
            segment_time_seconds,
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output_queue.put(line.rstrip())
            output_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        output_tail: list[str] = []
        stream_finished = False
        try:
            while process.poll() is None or not stream_finished:
                self._raise_if_cancelled()
                try:
                    line = output_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if line is None:
                    stream_finished = True
                    continue
                output_tail.append(line)
                output_tail = output_tail[-30:]
                if "time=" in line or "speed=" in line:
                    logger.debug("ffmpeg: %s", line)
        except KeyboardInterrupt:
            logger.warning("Stopping active FFmpeg split")
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            reader.join(timeout=2)

        if process.returncode != 0:
            tail = "\n".join(output_tail[-15:])
            raise MediaSplitError(
                f"FFmpeg split failed with exit code {process.returncode}: {tail}"
            )
        if not list_path.is_file():
            raise MediaSplitError("FFmpeg completed but did not create parts.csv")

    def _clear_generated_files(self, work_directory: Path) -> None:
        if not work_directory.is_dir():
            return
        for path in work_directory.iterdir():
            if (
                path.is_file()
                and (
                    path.name == "parts.csv"
                    or path.name == "manifest.json"
                    or path.name == "manifest.json.tmp"
                    or (
                        path.name.startswith("part_")
                        and path.suffix.casefold() in {".mkv", ".srt"}
                    )
                )
            ):
                path.unlink()

    def _read_parts_csv(self, work_directory: Path) -> list[MediaPart]:
        list_path = work_directory / "parts.csv"
        parts: list[MediaPart] = []
        with list_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for index, row in enumerate(csv.reader(handle), start=1):
                if len(row) < 3:
                    raise MediaSplitError(f"Invalid FFmpeg segment list row: {row!r}")
                raw_path = Path(row[0])
                part_path = (
                    raw_path
                    if raw_path.is_absolute()
                    else work_directory / raw_path.name
                )
                start_seconds = float(row[1])
                end_seconds = float(row[2])
                if end_seconds <= start_seconds:
                    raise MediaSplitError(
                        f"Invalid boundaries for FFmpeg part {index}: {row!r}"
                    )
                if not part_path.is_file():
                    raise MediaSplitError(f"FFmpeg part is missing: {part_path}")
                actual_duration = self.duration_probe(part_path)
                if actual_duration <= 0:
                    raise MediaSplitError(
                        f"ffprobe returned invalid duration for {part_path}: "
                        f"{actual_duration}"
                    )
                parts.append(
                    MediaPart(
                        index=index,
                        path=part_path,
                        start_seconds=start_seconds,
                        end_seconds=end_seconds,
                        duration_seconds=actual_duration,
                        size_bytes=part_path.stat().st_size,
                    )
                )
        if not parts:
            raise MediaSplitError("FFmpeg created no media parts")
        return parts

    @staticmethod
    def _largest_limit_ratio(
        parts: list[MediaPart],
        constraints: SplitConstraints,
    ) -> float:
        ratios = [1.0]
        for part in parts:
            if constraints.hard_max_duration_seconds is not None:
                ratios.append(
                    part.duration_seconds / constraints.hard_max_duration_seconds
                )
            if constraints.hard_max_size_bytes is not None:
                ratios.append(part.size_bytes / constraints.hard_max_size_bytes)
        return max(ratios)

    def _attach_srt(
        self,
        parts: list[MediaPart],
        srt_path: Path | None,
    ) -> list[MediaPart]:
        if srt_path is None or not Path(srt_path).is_file():
            return parts
        windows = [
            SegmentWindow(part.index, part.start_seconds, part.end_seconds)
            for part in parts
        ]
        outputs = [
            part.path.with_name(f"{part.path.stem}_chat.srt")
            for part in parts
        ]
        try:
            split_srt_file(Path(srt_path), windows, outputs)
        except (OSError, SRTError) as exc:
            logger.warning(
                "Skipping invalid/unreadable chat captions while keeping media "
                "parts uploadable: %s: %s",
                srt_path,
                exc,
            )
            for output in outputs:
                if output.is_file():
                    output.unlink()
            return parts
        return [
            MediaPart(
                index=part.index,
                path=part.path,
                start_seconds=part.start_seconds,
                end_seconds=part.end_seconds,
                duration_seconds=part.duration_seconds,
                size_bytes=part.size_bytes,
                srt_path=outputs[position],
            )
            for position, part in enumerate(parts)
        ]

    def _manifest_payload(
        self,
        source: Path,
        platform: str,
        segment_time_seconds: float,
        constraints: SplitConstraints,
        parts: list[MediaPart],
        srt_path: Path | None,
    ) -> dict[str, Any]:
        return {
            "version": MANIFEST_VERSION,
            "source": _source_identity(source),
            "srt_source": (
                _source_identity(Path(srt_path))
                if srt_path is not None and Path(srt_path).is_file()
                else None
            ),
            "platform": platform,
            "constraints": _constraints_dict(constraints),
            "segment_time_seconds": segment_time_seconds,
            "parts": [
                {
                    "index": part.index,
                    "path": part.path.name,
                    "start_seconds": part.start_seconds,
                    "end_seconds": part.end_seconds,
                    "duration_seconds": part.duration_seconds,
                    "size_bytes": part.size_bytes,
                    "srt_path": part.srt_path.name if part.srt_path else None,
                }
                for part in parts
            ],
        }

    def _write_manifest(self, manifest_path: Path, payload: dict[str, Any]) -> None:
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(manifest_path)

    def _load_manifest(
        self,
        source: Path,
        platform: str,
        constraints: SplitConstraints,
        work_directory: Path,
        srt_path: Path | None,
    ) -> SplitPlan | None:
        manifest_path = work_directory / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                payload.get("version") != MANIFEST_VERSION
                or payload.get("source") != _source_identity(source)
                or payload.get("srt_source")
                != (
                    _source_identity(Path(srt_path))
                    if srt_path is not None and Path(srt_path).is_file()
                    else None
                )
                or payload.get("platform") != platform
                or payload.get("constraints") != _constraints_dict(constraints)
            ):
                return None
            parts: list[MediaPart] = []
            for item in payload["parts"]:
                path = work_directory / item["path"]
                srt_path = (
                    work_directory / item["srt_path"]
                    if item.get("srt_path")
                    else None
                )
                if (
                    not path.is_file()
                    or path.stat().st_size != item["size_bytes"]
                    or (srt_path is not None and not srt_path.is_file())
                ):
                    return None
                parts.append(
                    MediaPart(
                        index=item["index"],
                        path=path,
                        start_seconds=item["start_seconds"],
                        end_seconds=item["end_seconds"],
                        duration_seconds=item["duration_seconds"],
                        size_bytes=item["size_bytes"],
                        srt_path=srt_path,
                    )
                )
            return SplitPlan(
                source_path=source,
                platform=platform,
                work_directory=work_directory,
                manifest_path=manifest_path,
                segment_time_seconds=payload["segment_time_seconds"],
                parts=tuple(parts),
                reused=True,
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Ignoring invalid split manifest: %s", manifest_path)
            return None

    def create_plan(
        self,
        source_path: Path,
        platform: str,
        duration_seconds: float,
        constraints: SplitConstraints,
        *,
        srt_path: Path | None = None,
    ) -> SplitPlan:
        """Create or reuse verified lossless media parts for one platform."""
        source = Path(source_path)
        if not source.is_file():
            raise FileNotFoundError(f"Source video was not found: {source}")
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")
        work_directory = self._work_directory(source, platform)
        reused = self._load_manifest(
            source,
            platform,
            constraints,
            work_directory,
            srt_path,
        )
        if reused is not None:
            logger.info(
                "Reusing %d verified %s split parts from %s",
                len(reused.parts),
                platform,
                reused.manifest_path,
            )
            return reused

        self._check_disk_space(source, work_directory)
        work_directory.mkdir(parents=True, exist_ok=True)
        segment_time = self._initial_segment_time(
            source,
            duration_seconds,
            constraints,
        )
        source_identity = _source_identity(source)

        for attempt in range(self.max_replans + 1):
            self._raise_if_cancelled()
            self._clear_generated_files(work_directory)
            self._run_ffmpeg(source, work_directory, segment_time)
            if _source_identity(source) != source_identity:
                raise MediaSplitError(
                    f"Source changed while FFmpeg was splitting it: {source}"
                )
            parts = self._read_parts_csv(work_directory)
            ratio = self._largest_limit_ratio(parts, constraints)
            if ratio <= 1.0:
                parts = self._attach_srt(parts, srt_path)
                payload = self._manifest_payload(
                    source,
                    platform,
                    segment_time,
                    constraints,
                    parts,
                    srt_path,
                )
                manifest_path = work_directory / "manifest.json"
                self._write_manifest(manifest_path, payload)
                logger.info(
                    "Created %d verified %s parts in %s",
                    len(parts),
                    platform,
                    work_directory,
                )
                return SplitPlan(
                    source_path=source,
                    platform=platform,
                    work_directory=work_directory,
                    manifest_path=manifest_path,
                    segment_time_seconds=segment_time,
                    parts=tuple(parts),
                )
            if attempt >= self.max_replans:
                break
            new_segment_time = segment_time / ratio * 0.9
            logger.warning(
                "%s part exceeded a hard limit by %.2f%%; replanning from "
                "%.3f s to %.3f s",
                platform,
                (ratio - 1) * 100,
                segment_time,
                new_segment_time,
            )
            segment_time = new_segment_time

        raise MediaSplitError(
            f"Could not produce {platform} parts within hard limits after "
            f"{self.max_replans + 1} plans"
        )

    def cleanup(self, plan: SplitPlan) -> None:
        """Remove only files owned by one verified plan after global success."""
        self._clear_generated_files(plan.work_directory)
        try:
            plan.work_directory.rmdir()
            plan.work_directory.parent.rmdir()
            plan.work_directory.parent.parent.rmdir()
        except OSError:
            pass
