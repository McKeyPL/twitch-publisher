"""Reczne czyszczenie starych nagran z katalogow ``_uploaded``."""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from config import load_config


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupReport:
    candidates: tuple[Path, ...]
    deleted: tuple[Path, ...]
    total_bytes: int
    dry_run: bool

    @property
    def total_megabytes(self) -> float:
        return self.total_bytes / (1024 * 1024)


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


def _assert_inside(path: Path, directory: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"Odmowa operacji poza katalogiem _uploaded: {path}") from exc


def _recording_files(video: Path) -> tuple[Path, ...]:
    companions = (
        video.with_name(f"{video.stem}_chat.srt"),
        video.with_name(f"{video.stem}_meta.txt"),
    )
    return (video, *(path for path in companions if path.is_file()))


def cleanup_uploaded_recordings(
    recordings_root: Path,
    uploaded_directory_name: str,
    retention_days: int,
    *,
    dry_run: bool = True,
    now: float | None = None,
) -> CleanupReport:
    """Kwalifikuje lub usuwa stare komplety plikow z katalogow uploadu."""
    if retention_days <= 0:
        raise ValueError("retention_days musi byc wieksze od zera")
    uploaded_name = _validate_uploaded_directory_name(uploaded_directory_name)
    root = Path(recordings_root).resolve(strict=False)
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    candidates: list[Path] = []
    deleted: list[Path] = []
    total_bytes = 0

    if not root.is_dir():
        logger.info("Katalog nagran nie istnieje: %s", root)
        return CleanupReport((), (), 0, dry_run)

    uploaded_directories = sorted(
        (
            channel / uploaded_name
            for channel in root.iterdir()
            if channel.is_dir() and (channel / uploaded_name).is_dir()
        ),
        key=lambda path: str(path).casefold(),
    )
    for uploaded_directory in uploaded_directories:
        _assert_inside(uploaded_directory, root)
        for video in sorted(uploaded_directory.glob("*.mkv")):
            _assert_inside(video, uploaded_directory)
            try:
                if video.stat().st_mtime >= cutoff:
                    continue
                files = _recording_files(video)
                for path in files:
                    _assert_inside(path, uploaded_directory)
                    size = path.stat().st_size
                    candidates.append(path)
                    total_bytes += size
                    if dry_run:
                        logger.info("DRY-RUN: usunieto by %s", path)
                    else:
                        path.unlink()
                        deleted.append(path)
                        logger.info("Usunieto %s", path)
            except OSError as exc:
                logger.error("Nie mozna przetworzyc %s: %s", video, exc)

    logger.info(
        "Cleanup: kandydaci=%d, usuniete=%d, rozmiar=%.2f MB, dry_run=%s",
        len(candidates),
        len(deleted),
        total_bytes / (1024 * 1024),
        dry_run,
    )
    return CleanupReport(tuple(candidates), tuple(deleted), total_bytes, dry_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml", type=Path)
    parser.add_argument("--retention-days", type=int)
    dry_group = parser.add_mutually_exclusive_group()
    dry_group.add_argument("--dry-run", dest="dry_run", action="store_true")
    dry_group.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    parser.set_defaults(dry_run=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    logging.basicConfig(level=getattr(logging, config.logging.level.upper(), logging.INFO))
    retention_days = (
        config.cleanup.retention_days
        if args.retention_days is None
        else args.retention_days
    )
    dry_run = config.cleanup.dry_run if args.dry_run is None else args.dry_run
    cleanup_uploaded_recordings(
        config.paths.recordings_root,
        config.moving.uploaded_directory_name,
        retention_days,
        dry_run=dry_run,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
