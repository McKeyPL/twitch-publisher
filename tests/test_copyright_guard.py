from __future__ import annotations

import threading
from unittest.mock import Mock

from copyright_guard import _force_exit_if_stuck, build_parser


def test_forced_stop_watchdog_does_nothing_after_clean_shutdown() -> None:
    shutdown_complete = threading.Event()
    shutdown_complete.set()
    exit_function = Mock()

    _force_exit_if_stuck(shutdown_complete, 0, exit_function)

    exit_function.assert_not_called()


def test_forced_stop_watchdog_uses_exit_130_when_shutdown_is_stuck() -> None:
    exit_function = Mock()

    _force_exit_if_stuck(threading.Event(), 0, exit_function)

    exit_function.assert_called_once_with(130)


def test_retry_reset_video_option_is_repeatable() -> None:
    args = build_parser().parse_args(
        ["--reset-video", "first", "--reset-video", "second"]
    )

    assert args.reset_video == ["first", "second"]


def test_channel_only_option_is_explicit() -> None:
    assert build_parser().parse_args(["--channel-only"]).channel_only
