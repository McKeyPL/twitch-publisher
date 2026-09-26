from __future__ import annotations

from pathlib import Path
import threading
from unittest.mock import MagicMock, Mock, patch

from config import load_config
from copyright_guard import _force_exit_if_stuck, build_parser, run


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def test_browser_debug_and_trace_are_separate_options() -> None:
    debug_only = build_parser().parse_args(["--browser-debug"])
    trace = build_parser().parse_args(["--browser-trace"])

    assert debug_only.browser_debug is True
    assert debug_only.browser_trace is False
    assert trace.browser_trace is True


def test_guard_returns_exit_130_after_keyboard_interrupt(tmp_path: Path) -> None:
    with patch.dict(
        "os.environ",
        {
            "YOUTUBE_CLIENT_SECRETS_FILE": "auth/credentials.json",
            "RECORDINGS_ROOT": str(tmp_path / "recordings"),
        },
        clear=True,
    ):
        config = load_config(
            PROJECT_ROOT / "config.yaml",
            dotenv_path=tmp_path / "missing.env",
        )
    service = MagicMock()
    service.run_cycle.side_effect = KeyboardInterrupt
    memory_guard = MagicMock()

    with (
        patch("copyright_guard.configure_logging"),
        patch("copyright_guard.prune_diagnostics", return_value=[]),
        patch("copyright_guard.MemoryGuard", return_value=memory_guard),
        patch("copyright_guard.StateStore"),
        patch("copyright_guard.CopyrightStateStore"),
        patch("copyright_guard.CopyrightGuardService", return_value=service),
        patch("copyright_guard.signal.signal"),
    ):
        assert run(config, once=False) == 130

    service.run_cycle.assert_called_once()
