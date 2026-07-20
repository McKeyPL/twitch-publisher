"""Parse ``*_meta.txt`` files generated after a stream ends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class MetadataError(ValueError):
    """The metadata file exists but has an invalid format."""


@dataclass(frozen=True, slots=True)
class StreamMetadata:
    channel: str
    title: str
    game: str | None
    started: datetime
    ended: datetime | None
    quality: str | None
    source_path: Path

    @property
    def duration_seconds(self) -> float | None:
        """Metadata duration; ffprobe remains authoritative for the MKV file."""
        if self.ended is None:
            return None
        return (self.ended - self.started).total_seconds()


def _read_key_values(text: str) -> dict[str, str]:
    """Read ``Key : value`` lines and indented value continuations."""
    values: dict[str, str] = {}
    current_key: str | None = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue

        # Indentation continues the previous value (for example a long title),
        # even when that value contains a colon.
        if raw_line[:1].isspace() and current_key is not None:
            continuation = raw_line.strip()
            if continuation:
                values[current_key] = f"{values[current_key]} {continuation}".strip()
            continue

        if ":" in raw_line:
            key_part, value_part = raw_line.split(":", 1)
            key = key_part.strip().casefold()
            if key:
                values[key] = value_part.strip()
                current_key = key
                continue

        raise MetadataError(f"Invalid line {line_number}: {raw_line!r}")

    return values


def _parse_datetime(value: str, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise MetadataError(
            f"Field {field_name!r} is not an ISO-8601 date: {value!r}"
        ) from exc


def parse_meta_file(
    path: str | Path,
    *,
    expected_channel: str | None = None,
) -> StreamMetadata:
    """Parse metadata and optionally validate the channel against its folder."""
    source_path = Path(path)
    try:
        text = source_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise MetadataError(f"Cannot read {source_path}: {exc}") from exc

    values = _read_key_values(text)
    missing = [name for name in ("channel", "title", "started") if not values.get(name)]
    if missing:
        raise MetadataError(f"Required fields are missing: {', '.join(missing)}")

    channel = values["channel"].strip()
    if expected_channel and channel.casefold() != expected_channel.strip().casefold():
        raise MetadataError(
            f"Channel {channel!r} does not match folder {expected_channel!r}"
        )

    started = _parse_datetime(values["started"], "Started")
    ended_value = values.get("ended", "").strip()
    ended = _parse_datetime(ended_value, "Ended") if ended_value else None
    if ended is not None and ended < started:
        raise MetadataError("Ended is earlier than Started")

    return StreamMetadata(
        channel=channel,
        title=values["title"].strip(),
        game=values.get("game", "").strip() or None,
        started=started,
        ended=ended,
        quality=values.get("quality", "").strip() or None,
        source_path=source_path,
    )


def meta_path_for_video(video_path: str | Path) -> Path:
    """Return the metadata path following the ``video_meta.txt`` convention."""
    video = Path(video_path)
    return video.with_name(f"{video.stem}_meta.txt")
