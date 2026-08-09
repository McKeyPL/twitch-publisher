from __future__ import annotations

import threading
from unittest.mock import Mock

from copyright_guard import _force_exit_if_stuck


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
