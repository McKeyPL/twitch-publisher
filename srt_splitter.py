"""Parse and split SubRip captions using actual media segment boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


TIMECODE_RE = re.compile(
    r"^(?P<start>\d{1,}:\d{2}:\d{2},\d{3})\s+-->\s+"
    r"(?P<end>\d{1,}:\d{2}:\d{2},\d{3})(?P<settings>\s+.*)?$"
)


class SRTError(ValueError):
    """Raised when an SRT file cannot be parsed safely."""


@dataclass(frozen=True, slots=True)
class SRTCue:
    start_ms: int
    end_ms: int
    lines: tuple[str, ...]
    settings: str = ""


@dataclass(frozen=True, slots=True)
class SegmentWindow:
    index: int
    start_seconds: float
    end_seconds: float

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ValueError("Segment index must be >= 1")
        if self.start_seconds < 0:
            raise ValueError("Segment start must be >= 0")
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Segment end must be greater than its start")


@dataclass(frozen=True, slots=True)
class SRTSplitResult:
    path: Path
    cue_count: int


def _parse_timestamp(value: str) -> int:
    hours, minutes, second_part = value.split(":")
    seconds, milliseconds = second_part.split(",")
    if int(minutes) > 59 or int(seconds) > 59:
        raise SRTError(f"Invalid SRT timestamp: {value}")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(milliseconds)
    )


def _format_timestamp(milliseconds: int) -> str:
    if milliseconds < 0:
        raise ValueError("SRT timestamp cannot be negative")
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def parse_srt_text(text: str) -> list[SRTCue]:
    """Parse UTF-8-decoded SubRip text and reject ambiguous malformed cues."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if not normalized.strip():
        return []

    cues: list[SRTCue] = []
    blocks = re.split(r"\n[ \t]*\n", normalized.strip())
    for block_number, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if not lines:
            continue
        timecode_index = 1 if lines[0].strip().isdigit() else 0
        if timecode_index >= len(lines):
            raise SRTError(f"SRT block {block_number} has no timecode")
        match = TIMECODE_RE.fullmatch(lines[timecode_index].strip())
        if match is None:
            raise SRTError(
                f"SRT block {block_number} has an invalid timecode: "
                f"{lines[timecode_index]!r}"
            )
        start_ms = _parse_timestamp(match.group("start"))
        end_ms = _parse_timestamp(match.group("end"))
        if end_ms <= start_ms:
            raise SRTError(
                f"SRT block {block_number} ends before or at its start"
            )
        caption_lines = tuple(lines[timecode_index + 1 :])
        if not caption_lines or not any(line.strip() for line in caption_lines):
            raise SRTError(f"SRT block {block_number} has no caption text")
        cues.append(
            SRTCue(
                start_ms=start_ms,
                end_ms=end_ms,
                lines=caption_lines,
                settings=(match.group("settings") or "").rstrip(),
            )
        )
    return cues


def read_srt(path: Path) -> list[SRTCue]:
    """Read a plain UTF-8/UTF-8-BOM SRT file."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise SRTError(f"SRT is not valid UTF-8: {source}: {exc}") from exc
    return parse_srt_text(text)


def render_srt(cues: Iterable[SRTCue]) -> str:
    """Render cues as normalized UTF-8 SubRip text with sequential indexes."""
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        timecode = (
            f"{_format_timestamp(cue.start_ms)} --> "
            f"{_format_timestamp(cue.end_ms)}{cue.settings}"
        )
        blocks.append("\n".join((str(index), timecode, *cue.lines)))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def cues_for_window(cues: Iterable[SRTCue], window: SegmentWindow) -> list[SRTCue]:
    """Clip overlapping cues to a segment and reset them to segment-local time."""
    window_start_ms = round(window.start_seconds * 1_000)
    window_end_ms = round(window.end_seconds * 1_000)
    result: list[SRTCue] = []
    for cue in cues:
        overlap_start = max(cue.start_ms, window_start_ms)
        overlap_end = min(cue.end_ms, window_end_ms)
        if overlap_end <= overlap_start:
            continue
        result.append(
            SRTCue(
                start_ms=overlap_start - window_start_ms,
                end_ms=overlap_end - window_start_ms,
                lines=cue.lines,
                settings=cue.settings,
            )
        )
    return result


def split_srt_file(
    source_path: Path,
    windows: Sequence[SegmentWindow],
    output_paths: Sequence[Path],
) -> list[SRTSplitResult]:
    """Split one SRT into files aligned to the supplied actual media windows."""
    if len(windows) != len(output_paths):
        raise ValueError("Each segment window must have exactly one SRT output path")
    cues = read_srt(source_path)
    results: list[SRTSplitResult] = []
    for window, output_path in zip(windows, output_paths):
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        segment_cues = cues_for_window(cues, window)
        output.write_text(render_srt(segment_cues), encoding="utf-8", newline="\n")
        results.append(SRTSplitResult(output, len(segment_cues)))
    return results
