"""Orkiestracja watchera, uploaderow, stanu SQLite i przenoszenia plikow."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from config import Config, load_config
from duration_check import (
    FileSizeStabilityTracker,
    ReadinessResult,
    ReadinessStatus,
    exceeds_duration_limit,
    probe_duration_seconds,
)
from meta_parser import StreamMetadata
from mover import move_processed_recording
from state import StateStore, UploadStatus
from title_cleaner import title_from_metadata
from uploaders.base import BaseUploader
from uploaders.cda import CDAUploader
from uploaders.rumble import RumbleUploader
from uploaders.youtube import YouTubeUploader
from watcher import scan_cycle


logger = logging.getLogger(__name__)
DurationProbe = Callable[[Path], float]
NO_AUTO_RETRY_PREFIX = "[NO_AUTO_RETRY] "


def _request_stop(
    stop_event: threading.Event,
    signum: int,
    frame: object,
) -> None:
    """SIGINT przerywa aktywna operacje; SIGTERM konczy po biezacym kroku."""
    if signum == getattr(signal, "SIGINT", None):
        stop_event.set()
        logger.info("Otrzymano SIGINT; przerywam aktywna operacje")
        raise KeyboardInterrupt
    logger.info("Otrzymano sygnal %s; koncze po biezacym kroku", signum)
    stop_event.set()


class _ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[35m",
    }

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{text}\033[0m" if color else text


def configure_logging(config: Config) -> None:
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    config.paths.log_directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(_ColorFormatter(formatter._fmt, datefmt=formatter.datefmt) if config.logging.console_colors else formatter)
    file_handler = logging.FileHandler(
        config.paths.log_directory / config.logging.file_name,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)


def build_uploaders(
    config: Config,
    state_store: StateStore,
    cancel_event: threading.Event | None = None,
) -> dict[str, BaseUploader]:
    uploaders: dict[str, BaseUploader] = {}
    if config.platforms.youtube.enabled:
        uploaders["youtube"] = YouTubeUploader(
            config.platforms.youtube,
            config.retry,
            state_store,
            cancel_event=cancel_event,
        )
    if config.platforms.cda.enabled:
        uploaders["cda"] = CDAUploader(
            config.platforms.cda,
            config.browser,
            config.retry,
            cancel_event=cancel_event,
        )
    if config.platforms.rumble.enabled:
        uploaders["rumble"] = RumbleUploader(
            config.platforms.rumble,
            config.browser,
            config.retry,
            cancel_event=cancel_event,
        )
    return uploaders


def _video_path_from_metadata(metadata: StreamMetadata) -> Path:
    suffix = "_meta.txt"
    name = metadata.source_path.name
    if not name.endswith(suffix):
        raise ValueError(f"Niepoprawna nazwa pliku metadanych: {metadata.source_path}")
    return metadata.source_path.with_name(f"{name[:-len(suffix)]}.mkv")


def _srt_path(video_path: Path) -> Path | None:
    path = video_path.with_name(f"{video_path.stem}_chat.srt")
    return path if path.is_file() and path.stat().st_size > 0 else None


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def build_description(metadata: StreamMetadata, duration_seconds: float) -> str:
    lines = [
        f"Nagranie streama: {metadata.channel}",
        f"Data rozpoczecia: {metadata.started.isoformat(sep=' ')}",
        f"Czas trwania: {_format_duration(duration_seconds)}",
    ]
    if metadata.game:
        lines.append(f"Gra/kategoria: {metadata.game}")
    return "\n".join(lines)


def build_tags(metadata: StreamMetadata) -> list[str]:
    tags = [metadata.channel, "Twitch", "stream"]
    if metadata.game:
        tags.append(metadata.game)
    return tags


def _platform_limit(config: Config, platform: str) -> float | None:
    return getattr(config.platforms, platform).max_duration_hours


def _platform_title_limit(config: Config, platform: str) -> int | None:
    return getattr(config.platforms, platform).title_limit


def _mark_exception_failed(
    state_store: StateStore,
    video_path: Path,
    platform: str,
    exc: Exception,
) -> None:
    message = f"Nieoczekiwany blad uploadera: {exc}"
    try:
        state_store.mark_failed(video_path, platform, message)
    except Exception:
        logger.exception("Nie udalo sie zapisac statusu FAILED dla %s/%s", video_path, platform)
    logger.exception("%s: %s", platform, message)


def process_ready_recording(
    video_path: Path,
    metadata: StreamMetadata,
    duration_seconds: float,
    config: Config,
    state_store: StateStore,
    uploaders: Mapping[str, BaseUploader],
) -> None:
    """Przetwarza jedno gotowe nagranie, izolujac kazda platforme."""
    required_platforms = list(uploaders)
    description = build_description(metadata, duration_seconds)
    tags = build_tags(metadata)
    srt_path = _srt_path(video_path)

    for platform, uploader in uploaders.items():
        try:
            current = state_store.get_status(video_path, platform)
            if current is not None and current.status in {
                UploadStatus.SUCCESS,
                UploadStatus.SKIPPED,
            }:
                logger.info("%s: pomijam zakonczony status %s", platform, current.status.value)
                continue
            if (
                current is not None
                and current.status is UploadStatus.FAILED
                and (current.last_error or "").startswith(NO_AUTO_RETRY_PREFIX)
            ):
                logger.warning(
                    "%s: wymagana reczna weryfikacja poprzedniego uploadu; "
                    "nie ponawiam automatycznie: %s",
                    platform,
                    current.last_error,
                )
                continue

            limit = _platform_limit(config, platform)
            if limit is not None and exceeds_duration_limit(duration_seconds, limit):
                reason = f"Czas {_format_duration(duration_seconds)} przekracza limit {limit:g} h"
                state_store.mark_skipped(video_path, platform, reason)
                logger.warning("%s: %s", platform, reason)
                continue

            title = title_from_metadata(
                metadata,
                config.metadata.title_template,
                _platform_title_limit(config, platform),
            )
            state_store.mark_in_progress(video_path, platform)
            result = uploader.upload(video_path, title, description, tags, srt_path)
            if not result.success:
                error = result.error_message or "Uploader zwrocil success=False"
                if not result.retry_allowed:
                    error = f"{NO_AUTO_RETRY_PREFIX}{error}"
                state_store.mark_failed(video_path, platform, error)
                logger.error("%s: upload %s nie powiodl sie: %s", platform, video_path, error)
                continue

            state_store.mark_success(video_path, platform, result.platform_video_id)
            if result.captions_uploaded:
                state_store.mark_captions_uploaded(video_path, platform)

            if platform == "youtube" and metadata.channel in config.platforms.youtube.playlists:
                playlist_id = config.platforms.youtube.playlists[metadata.channel]
                if uploader.add_to_playlist(
                    result.platform_video_id or "",
                    playlist_id,
                    playlist_title=metadata.channel,
                ):
                    state_store.mark_playlist_added(video_path, platform)
        except Exception as exc:
            _mark_exception_failed(state_store, video_path, platform, exc)

    move_result = move_processed_recording(
        video_path, config, state_store, required_platforms
    )
    if move_result.moved:
        logger.info("Przeniesiono przetworzone nagranie do %s", move_result.destination)
    for warning in move_result.warnings:
        logger.warning("Mover: %s", warning)


def process_readiness_results(
    results: Sequence[ReadinessResult],
    config: Config,
    state_store: StateStore,
    uploaders: Mapping[str, BaseUploader],
    *,
    duration_probe: DurationProbe | None = None,
) -> None:
    probe = duration_probe or (
        lambda path: probe_duration_seconds(path, ffprobe_path=config.paths.ffprobe)
    )
    for result in results:
        if result.status is not ReadinessStatus.READY or result.metadata is None:
            continue
        try:
            video_path = _video_path_from_metadata(result.metadata)
            duration = probe(video_path)
            process_ready_recording(
                video_path, result.metadata, duration, config, state_store, uploaders
            )
        except Exception:
            logger.exception("Nieoczekiwany blad przetwarzania gotowego nagrania")


def run_cycle(
    config: Config,
    state_store: StateStore,
    tracker: FileSizeStabilityTracker,
    uploaders: Mapping[str, BaseUploader],
    *,
    now: float | None = None,
    duration_probe: DurationProbe | None = None,
) -> list[ReadinessResult]:
    results = scan_cycle(
        config.paths.recordings_root,
        tracker,
        config.moving.uploaded_directory_name,
        now=now,
    )
    process_readiness_results(
        results, config, state_store, uploaders, duration_probe=duration_probe
    )
    return results


def run(config: Config, *, once: bool = False) -> int:
    configure_logging(config)
    stop_event = threading.Event()

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(
                getattr(signal, signal_name),
                lambda signum, frame: _request_stop(stop_event, signum, frame),
            )

    with StateStore(config.paths.database) as store:
        uploaders = build_uploaders(config, store, stop_event)
        tracker = FileSizeStabilityTracker(config.watcher.size_stability_seconds)
        try:
            while not stop_event.is_set():
                try:
                    run_cycle(config, store, tracker, uploaders)
                except Exception:
                    # Awaria calego skanu (np. chwilowy blad dysku) nie moze
                    # zakonczyc dlugo dzialajacego procesu.
                    logger.exception("Nieoczekiwany blad cyklu; sprobuje ponownie")
                if once:
                    break
                stop_event.wait(config.watcher.poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Przerwano przez uzytkownika")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--browser-debug",
        action="store_true",
        help="Pokaz okno Playwright i zapisz trace/screenshoty diagnostyczne",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    if args.browser_debug:
        config = replace(
            config,
            browser=replace(config.browser, debug=True, headless=False),
        )
    return run(config, once=args.once)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
