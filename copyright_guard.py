"""Run the standalone YouTube copyright restriction guard."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable, Sequence

from config import Config, load_config
from state import StateStore
from youtube_copyright.service import CopyrightGuardService
from youtube_copyright.state import CopyrightStateStore
from youtube_copyright.browser_session import (
    StudioAuthRequired,
    StudioBrowserError,
    StudioBrowserManager,
)
from youtube_copyright.diagnostics import prune_diagnostics
from youtube_copyright.process_lock import GuardAlreadyRunning, SingleInstanceLock


logger = logging.getLogger(__name__)
_FORCED_STOP_TIMEOUT_SECONDS = 5.0


def _force_exit_if_stuck(
    shutdown_complete: threading.Event,
    timeout_seconds: float = _FORCED_STOP_TIMEOUT_SECONDS,
    exit_function: Callable[[int], None] | None = None,
) -> None:
    """Guarantee that an interrupted synchronous browser call cannot hang forever."""

    if shutdown_complete.wait(timeout_seconds):
        return
    logger.critical(
        "Copyright Guard did not stop within %.1f s after Ctrl+C; forcing exit",
        timeout_seconds,
    )
    (exit_function or os._exit)(130)


def configure_logging(config: Config) -> None:
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    config.paths.log_directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(
        config.paths.log_directory / "youtube_copyright_guard.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)


def run(
    config: Config,
    *,
    once: bool = False,
    video_ids: Sequence[str] = (),
) -> int:
    configure_logging(config)
    logger.info(
        "Copyright Guard started independently: mode=%s, once=%s, explicit_videos=%d",
        config.youtube_copyright.mode,
        once,
        len(video_ids),
    )
    removed = prune_diagnostics(config.youtube_copyright.diagnostics)
    if removed:
        logger.info("Pruned %d expired copyright diagnostic runs", len(removed))
    if not config.youtube_copyright.enabled:
        logger.info("YouTube Copyright Guard is disabled in config.yaml")
        return 0
    if not config.platforms.youtube.enabled:
        logger.error("YouTube platform must be enabled for copyright monitoring")
        return 2

    stop_event = threading.Event()
    shutdown_complete = threading.Event()
    interrupt_count = 0

    def request_stop(signum: int, frame: object) -> None:
        nonlocal interrupt_count
        interrupt_count += 1
        logger.info("Received signal %s; stopping the copyright guard", signum)
        stop_event.set()
        if interrupt_count >= 2:
            logger.critical("Received a second stop signal; forcing immediate exit")
            os._exit(130)
        threading.Thread(
            target=_force_exit_if_stuck,
            args=(shutdown_complete,),
            name="copyright-guard-stop-watchdog",
            daemon=True,
        ).start()
        raise KeyboardInterrupt

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), request_stop)

    try:
        try:
            logger.info(
                "Opening Copyright Guard state and publisher quota database: %s",
                config.paths.database,
            )
            with (
                StateStore(config.paths.database) as quota_store,
                CopyrightStateStore(config.paths.database) as copyright_store,
            ):
                logger.info("Copyright Guard state databases are ready")
                service = CopyrightGuardService(
                    config,
                    copyright_store,
                    quota_store,
                    stop_event=stop_event,
                )
                while not stop_event.is_set():
                    try:
                        result = service.run_cycle(
                            video_ids=video_ids or None,
                            include_channel_uploads=not video_ids,
                        )
                        logger.info(
                            "Copyright cycle %s finished: checked=%d actionable=%d "
                            "submitted=%d ignored=%d missing=%d",
                            result.run_id,
                            result.videos_checked,
                            len(result.actionable_video_ids),
                            result.actions_submitted,
                            len(result.ignored_video_ids),
                            len(result.missing_video_ids),
                        )
                        if (
                            config.youtube_copyright.mode == "automatic"
                            and result.actionable_video_ids
                            and result.actions_submitted == 0
                        ):
                            logger.warning(
                                "The cycle found actionable restrictions but submitted "
                                "no Studio edits; inspect per-video errors and diagnostics"
                            )
                    except Exception:
                        logger.exception(
                            "Copyright guard cycle failed; the next cycle will retry"
                        )
                        if once:
                            return 1
                    if once:
                        return 0
                    stop_event.wait(config.youtube_copyright.interval_hours * 3600)
        except KeyboardInterrupt:
            logger.info("Copyright Guard was interrupted by the user")
        return 0
    finally:
        shutdown_complete.set()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--video-id", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--browser-debug", action="store_true")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open a visible browser and create/refresh the YouTube Studio session",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    copyright_config = config.youtube_copyright
    if args.dry_run:
        copyright_config = replace(copyright_config, mode="dry_run")
    if args.browser_debug:
        copyright_config = replace(
            copyright_config,
            browser=replace(
                copyright_config.browser,
                headless=False,
                trace_mode="always",
                screenshots=True,
                console_logging=True,
                failed_request_logging=True,
            ),
        )
    config = replace(config, youtube_copyright=copyright_config)
    if args.login:
        copyright_config = replace(
            copyright_config,
            browser=replace(copyright_config.browser, headless=False),
        )
        config = replace(config, youtube_copyright=copyright_config)
        configure_logging(config)
        manager = StudioBrowserManager(
            config.youtube_copyright.browser,
            config.youtube_copyright.diagnostics,
        )
        lock_path = config.paths.database.parent / "youtube_copyright_guard.lock"
        try:
            with SingleInstanceLock(lock_path):
                manager.login("interactive-login")
                logger.info("YouTube Studio session was saved successfully")
        except (GuardAlreadyRunning, StudioAuthRequired, StudioBrowserError) as exc:
            logger.error("YouTube Studio login failed: %s", exc)
            return 2
        return 0
    lock_path = config.paths.database.parent / "youtube_copyright_guard.lock"
    try:
        with SingleInstanceLock(lock_path):
            return run(config, once=args.once, video_ids=args.video_id)
    except GuardAlreadyRunning as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
