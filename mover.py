"""Przenoszenie kompletnie przetworzonych nagran do katalogu ``_uploaded``."""

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
        raise ValueError("uploaded_directory_name musi byc pojedyncza nazwa katalogu")
    return cleaned


def _move_companions(
    sources: Sequence[Path],
    destination_directory: Path,
) -> list[str]:
    """Przenosi opcjonalne pliki, kontynuujac po kazdym pojedynczym bledzie."""
    warnings: list[str] = []
    for source in sources:
        destination = destination_directory / source.name

        if destination.exists():
            if source.exists():
                warning = (
                    f"Plik towarzyszacy istnieje jednoczesnie w zrodle i celu: "
                    f"{source}; wymagana manualna interwencja"
                )
                warnings.append(warning)
                logger.warning(warning)
            continue

        if not source.exists():
            continue

        try:
            if source.suffix.casefold() == ".srt" and source.stat().st_size == 0:
                logger.info("Przenoszenie pustego pliku napisow SRT: %s", source)
            shutil.move(str(source), str(destination))
        except (OSError, shutil.Error) as exc:
            warning = (
                f"Nie przeniesiono pliku towarzyszacego {source} do "
                f"{destination}: {exc}; wymagana manualna interwencja"
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
    """Przenosi MKV i obecne pliki towarzyszace po sukcesie wszystkich platform.

    MKV jest przenoszony pierwszy. Po jego sukcesie problemy z SRT/meta nie
    uruchamiaja rollbacku nagrania; sa raportowane w ``MoveResult.warnings``.
    """
    video = Path(video_path).expanduser().resolve(strict=False)

    try:
        fully_processed = state_store.is_fully_processed(video, required_platforms)
    except Exception as exc:
        reason = f"Nie mozna sprawdzic statusu platform dla {video}: {exc}"
        logger.exception(reason)
        return MoveResult(moved=False, reason=reason)

    if not fully_processed:
        reason = "Nie wszystkie wymagane platformy maja status SUCCESS albo SKIPPED"
        logger.info("%s: %s", reason, video)
        return MoveResult(moved=False, reason=reason)

    try:
        uploaded_directory_name = _validate_uploaded_directory_name(
            config.moving.uploaded_directory_name
        )
    except (AttributeError, TypeError, ValueError) as exc:
        reason = f"Niepoprawna konfiguracja katalogu docelowego: {exc}"
        logger.error(reason)
        return MoveResult(moved=False, reason=reason)

    destination_directory = video.parent / uploaded_directory_name
    destination_video = destination_directory / video.name
    companion_sources = _companion_paths(video)

    # Poprzednia operacja mogla przeniesc MKV i zakonczyc proces przed SRT/meta.
    if not video.exists() and destination_video.is_file():
        try:
            destination_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # praktycznie tylko race/uprawnienia do katalogu
            reason = f"Nie mozna otworzyc katalogu docelowego {destination_directory}: {exc}"
            logger.error(reason)
            return MoveResult(moved=False, reason=reason)

        warnings = _move_companions(companion_sources, destination_directory)
        logger.info("Nagranie bylo juz przeniesione: %s", destination_video)
        return MoveResult(
            moved=True,
            already_done=True,
            reason="Plik MKV znajduje sie juz w katalogu docelowym",
            destination=destination_video,
            warnings=warnings,
        )

    if not video.is_file():
        reason = (
            f"Plik MKV zniknal lub nie jest zwyklym plikiem: {video}; "
            "nie znaleziono go rowniez w katalogu docelowym"
        )
        logger.error(reason)
        return MoveResult(moved=False, reason=reason)

    if destination_video.exists():
        reason = (
            f"Plik MKV istnieje jednoczesnie w zrodle i celu: {destination_video}; "
            "odmowa nadpisania, wymagana manualna interwencja"
        )
        logger.error(reason)
        return MoveResult(moved=False, reason=reason, destination=destination_video)

    try:
        destination_directory.mkdir(parents=True, exist_ok=True)
        shutil.move(str(video), str(destination_video))
    except (OSError, shutil.Error) as exc:
        reason = f"Nie przeniesiono glownego pliku MKV {video}: {exc}"
        logger.exception(reason)
        return MoveResult(
            moved=False,
            reason=reason,
            destination=destination_video,
        )

    logger.info("Przeniesiono nagranie %s do %s", video, destination_video)
    warnings = _move_companions(companion_sources, destination_directory)
    return MoveResult(
        moved=True,
        destination=destination_video,
        warnings=warnings,
    )

