"""Move fully processed recordings into the ``_uploaded`` directory."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from config import Config
from state import StateStore


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MoveResult:
    moved: bool
    already_done: bool = False
    reason: str | None = None
    destination: Path | None = None
    warnings: list[str] = field(default_factory=list)


def _companion_paths(video_path: Path) -> tuple[Path, Path]:
    return (
        video_path.with_name(f"{video_path.stem}_chat.srt"),
        video_path.with_name(f"{video_path.stem}_meta.txt"),
    )


def _validate_uploaded_directory_name(name: str) -> str:
    cleaned = name.strip()
    candidate = Path(cleaned)
    if (
        not cleaned
        or candidate.is_absolute()
        or cleaned in {".", ".."}
        or candidate.name != cleaned
        or "/" in cleaned
        or "\\" in cleaned
    ):
        raise ValueError("uploaded_directory_name must be a single directory name")
    return cleaned


def _move_companions(
    sources: Sequence[Path],
    destination_directory: Path,
) -> list[str]:
    """Move optional companion files, continuing after individual failures."""
    warnings: list[str] = []
    for source in sources:
        destination = destination_directory / source.name

        if destination.exists():
            if source.exists():
                warning = (
                    f"Companion file exists at both source and destination: "
                    f"{source}; manual intervention is required"
                )
                warnings.append(warning)
                logger.warning(warning)
            continue

        if not source.exists():
            continue

        try:
            if source.suffix.casefold() == ".srt" and source.stat().st_size == 0:
                logger.info("Moving an empty SRT captions file: %s", source)
            shutil.move(str(source), str(destination))
        except (OSError, shutil.Error) as exc:
            warning = (
                f"Could not move companion file {source} to {destination}: {exc}; "
                "manual intervention is required"
            )
            warnings.append(warning)
            logger.warning(warning)

    return warnings


def move_processed_recording(
    video_path: Path,
    config: Config,
    state_store: StateStore,
    required_platforms: list[str],
) -> MoveResult:
    """Move the MKV and existing companion files after all platforms succeed.

    The MKV is moved first. Once it succeeds, SRT/metadata failures do not roll
    the video back; they are reported in ``MoveResult.warnings``.
    """
    video = Path(video_path).expanduser().resolve(strict=False)

    try:
        fully_processed = state_store.is_fully_processed(video, required_platforms)
    except Exception as exc:
        reason = f"Cannot check platform status for {video}: {exc}"
        logger.exception(reason)
        return MoveResult(moved=False, reason=reason)

    if not fully_processed:
        reason = "Not all required platforms have SUCCESS or SKIPPED status"
        logger.info("%s: %s", reason, video)
        return MoveResult(moved=False, reason=reason)

    try:
        uploaded_directory_name = _validate_uploaded_directory_name(
            config.moving.uploaded_directory_name
        )
    except (AttributeError, TypeError, ValueError) as exc:
        reason = f"Invalid destination-directory configuration: {exc}"
        logger.error(reason)
        return MoveResult(moved=False, reason=reason)

    destination_directory = video.parent / uploaded_directory_name
    destination_video = destination_directory / video.name
    companion_sources = _companion_paths(video)

    # A previous operation may have moved the MKV and exited before SRT/metadata.
    if not video.exists() and destination_video.is_file():
        try:
            destination_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # normally a race or directory permission issue
            reason = f"Cannot open destination directory {destination_directory}: {exc}"
            logger.error(reason)
            return MoveResult(moved=False, reason=reason)

        warnings = _move_companions(companion_sources, destination_directory)
        logger.info("Recording was already moved: %s", destination_video)
        return MoveResult(
            moved=True,
            already_done=True,
            reason="The MKV file is already in the destination directory",
            destination=destination_video,
            warnings=warnings,
        )

    if not video.is_file():
        reason = (
            f"The MKV disappeared or is not a regular file: {video}; "
            "it was not found in the destination directory either"
        )
        logger.error(reason)
        return MoveResult(moved=False, reason=reason)

    if destination_video.exists():
        reason = (
            f"The MKV exists at both source and destination: {destination_video}; "
            "refusing to overwrite, manual intervention is required"
        )
        logger.error(reason)
        return MoveResult(moved=False, reason=reason, destination=destination_video)

    try:
        destination_directory.mkdir(parents=True, exist_ok=True)
        shutil.move(str(video), str(destination_video))
    except (OSError, shutil.Error) as exc:
        reason = f"Could not move the main MKV file {video}: {exc}"
        logger.exception(reason)
        return MoveResult(
            moved=False,
            reason=reason,
            destination=destination_video,
        )

    logger.info("Moved recording %s to %s", video, destination_video)
    warnings = _move_companions(companion_sources, destination_directory)
    return MoveResult(
        moved=True,
        destination=destination_video,
        warnings=warnings,
    )
