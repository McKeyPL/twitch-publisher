"""Safely rename complete recording sets for legacy CDA filename handling."""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from state import StateStore


logger = logging.getLogger(__name__)

_BANG_COMMAND = re.compile(r"(?<!\S)![^\W_][\w-]*(?=\s|$)", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_SAFE_PUNCTUATION = frozenset(" _-.,()[]'&")
_COMPANION_SUFFIXES = ("_chat.srt", "_meta.txt")


class RecordingNameNormalizationError(RuntimeError):
    """A recording set could not be renamed without risking data or state."""


@dataclass(frozen=True, slots=True)
class RecordingNameResult:
    original_video_path: Path
    video_path: Path
    metadata_path: Path
    srt_path: Path | None
    renamed: bool
    migrated_statuses: int = 0


def _repair_utf8_mojibake(value: str) -> str:
    """Repair a reversible UTF-8 string that was decoded as Windows-1252."""
    markers = ("Ã", "Ä", "Å", "ð", "â")

    def repair_chunk(chunk: str) -> str:
        if not any(marker in chunk for marker in markers):
            return chunk
        try:
            candidate = chunk.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return chunk
        return candidate if "\ufffd" not in candidate else chunk

    output: list[str] = []
    cp1252_chunk: list[str] = []
    for character in value:
        try:
            character.encode("cp1252")
        except UnicodeEncodeError:
            output.append(repair_chunk("".join(cp1252_chunk)))
            cp1252_chunk.clear()
            output.append(character)
        else:
            cp1252_chunk.append(character)
    output.append(repair_chunk("".join(cp1252_chunk)))
    return "".join(output)


def sanitize_cda_filename_stem(value: str, *, max_length: int = 140) -> str:
    """Return a conservative BMP-only filename stem for CDA.

    CDA's older upload backend behaves like a legacy three-byte UTF-8 stack.
    Letters, combining marks, and numbers in the Basic Multilingual Plane are
    retained. Emoji, non-BMP code points, symbols, variation selectors, private
    use characters, controls, and unusual punctuation are replaced with spaces.
    """
    if not isinstance(value, str):
        raise TypeError("filename stem must be a string")
    if max_length <= 0:
        raise ValueError("max_length must be greater than zero")

    normalized = unicodedata.normalize("NFC", _repair_utf8_mojibake(value))
    normalized = _BANG_COMMAND.sub(" ", normalized)
    output: list[str] = []
    for character in normalized:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if character in _SAFE_PUNCTUATION:
            output.append(character)
        elif character.isspace():
            output.append(" ")
        elif (
            codepoint <= 0xFFFF
            and not 0xFE00 <= codepoint <= 0xFE0F
            and codepoint != 0x20E3
            and category[0] in {"L", "M", "N"}
            and category not in {"Cs", "Co", "Cn"}
        ):
            output.append(character)
        else:
            output.append(" ")

    cleaned = _WHITESPACE.sub(" ", "".join(output)).strip(" .-_")
    if not cleaned:
        cleaned = "recording"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .-_")
    return cleaned or "recording"


def _recording_paths(video_path: Path) -> tuple[Path, Path]:
    return (
        video_path.with_name(f"{video_path.stem}_chat.srt"),
        video_path.with_name(f"{video_path.stem}_meta.txt"),
    )


def _rollback_renames(renames: list[tuple[Path, Path]]) -> None:
    for source, target in reversed(renames):
        if target.is_file() and not source.exists():
            target.replace(source)


def normalize_recording_set_for_cda(
    video_path: str | Path,
    state_store: StateStore,
    *,
    max_stem_length: int = 140,
) -> RecordingNameResult:
    """Rename MKV/SRT/TXT together and migrate existing SQLite statuses."""
    original_video = Path(video_path).expanduser().resolve(strict=False)
    if not original_video.is_file():
        raise RecordingNameNormalizationError(
            f"Recording video does not exist: {original_video}"
        )

    clean_stem = sanitize_cda_filename_stem(
        original_video.stem,
        max_length=max_stem_length,
    )
    original_srt, original_metadata = _recording_paths(original_video)
    if clean_stem == original_video.stem:
        return RecordingNameResult(
            original_video_path=original_video,
            video_path=original_video,
            metadata_path=original_metadata,
            srt_path=original_srt if original_srt.is_file() else None,
            renamed=False,
        )

    target_video = original_video.with_name(f"{clean_stem}{original_video.suffix}")
    target_srt = original_video.with_name(f"{clean_stem}_chat.srt")
    target_metadata = original_video.with_name(f"{clean_stem}_meta.txt")
    pairs = [(original_video, target_video)]
    if original_srt.is_file():
        pairs.append((original_srt, target_srt))
    if original_metadata.is_file():
        pairs.append((original_metadata, target_metadata))

    for source, target in pairs:
        if target.exists() and target != source:
            raise RecordingNameNormalizationError(
                f"Refusing to overwrite an existing normalized file: {target}"
            )

    staged: list[tuple[Path, Path, Path]] = []
    finalized: list[tuple[Path, Path]] = []
    try:
        for source, target in pairs:
            temporary = source.with_name(
                f".twitch-publisher-normalize-{uuid.uuid4().hex}.tmp"
            )
            source.replace(temporary)
            staged.append((source, target, temporary))
        for source, target, temporary in staged:
            temporary.replace(target)
            finalized.append((source, target))
    except Exception as exc:
        for source, target, temporary in reversed(staged):
            current = target if (source, target) in finalized else temporary
            if current.exists() and not source.exists():
                current.replace(source)
        raise RecordingNameNormalizationError(
            f"Could not rename recording set {original_video}: {exc}"
        ) from exc

    try:
        migrated = state_store.migrate_video_path(original_video, target_video)
    except Exception as exc:
        try:
            _rollback_renames(finalized)
        except Exception as rollback_exc:
            raise RecordingNameNormalizationError(
                "SQLite migration and filename rollback both failed. "
                f"Migration: {exc}; rollback: {rollback_exc}"
            ) from exc
        raise RecordingNameNormalizationError(
            f"SQLite path migration failed; file renames were rolled back: {exc}"
        ) from exc

    logger.info(
        "Normalized recording filename for CDA: %s -> %s "
        "(migrated SQLite statuses: %d)",
        original_video.name,
        target_video.name,
        migrated,
    )
    return RecordingNameResult(
        original_video_path=original_video,
        video_path=target_video,
        metadata_path=target_metadata,
        srt_path=target_srt if target_srt.is_file() else None,
        renamed=True,
        migrated_statuses=migrated,
    )
