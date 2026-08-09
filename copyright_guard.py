"""Run the standalone YouTube copyright restriction guard."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from config import Config, load_config
from state import StateStore
from youtube_copyright.service import CopyrightGuardService
from youtube_copyright.state import CopyrightStateStore
from youtube_copyright.browser_session import StudioBrowserManager


logger = logging.getLogger(__name__)


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
    if not config.youtube_copyright.enabled:
        logger.info("YouTube Copyright Guard is disabled in config.yaml")
        return 0
    if not config.platforms.youtube.enabled:
        logger.error("YouTube platform must be enabled for copyright monitoring")
        return 2

    stop_event = threading.Event()

    def request_stop(signum: int, frame: object) -> None:
        logger.info("Received signal %s; stopping the copyright guard", signum)
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), request_stop)

    with (
        StateStore(config.paths.database) as quota_store,
        CopyrightStateStore(config.paths.database) as copyright_store,
    ):
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
                    "Copyright cycle %s finished: checked=%d actionable=%d ignored=%d missing=%d",
                    result.run_id,
                    result.videos_checked,
                    len(result.actionable_video_ids),
                    len(result.ignored_video_ids),
                    len(result.missing_video_ids),
                )
            except Exception:
                logger.exception("Copyright guard cycle failed; the next cycle will retry")
                if once:
                    return 1
            if once:
                return 0
            stop_event.wait(config.youtube_copyright.interval_hours * 3600)
    return 0


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
        configure_logging(config)
        manager = StudioBrowserManager(
            config.youtube_copyright.browser,
            config.youtube_copyright.diagnostics,
        )
        with manager.open("interactive-login", interactive_login=True):
            logger.info("YouTube Studio session was saved successfully")
        return 0
    return run(config, once=args.once, video_ids=args.video_id)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
