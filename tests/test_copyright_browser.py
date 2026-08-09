from __future__ import annotations

from pathlib import Path
import os
from unittest.mock import MagicMock, patch

import pytest

from config import CopyrightBrowserConfig, CopyrightDiagnosticsConfig
from youtube_copyright.browser_session import (
    StudioAuthRequired,
    StudioBrowserManager,
)
from youtube_copyright.diagnostics import (
    DiagnosticRun,
    prune_diagnostics,
    safe_component,
    safe_url,
)


def _browser_config(tmp_path: Path, *, trace_mode: str = "always"):
    return CopyrightBrowserConfig(
        storage_state_file=tmp_path / "auth" / "state.json",
        user_data_directory=tmp_path / "auth" / "profile",
        channel="chrome",
        executable_path=None,
        headless=False,
        locale="en-US",
        trace_mode=trace_mode,
        screenshots=True,
        console_logging=True,
        failed_request_logging=True,
        navigation_timeout_seconds=1,
        action_timeout_seconds=2,
    )


def _diagnostics(tmp_path: Path):
    return CopyrightDiagnosticsConfig(tmp_path / "logs", 14)


def test_safe_diagnostic_paths_and_url_redaction(tmp_path: Path) -> None:
    assert safe_component("../../video id") == "video_id"
    assert safe_url("https://studio.youtube.com/video/abc?token=secret#x") == (
        "https://studio.youtube.com/video/abc"
    )
    run = DiagnosticRun(_diagnostics(tmp_path), "run/1", "video:id")
    path = run.write_json("decision", {"action": "TRIM"})
    assert path.is_file()
    assert "run_1" in str(path)
    assert "video_id" in str(path)


def _playwright_fixture(authenticated: bool = True):
    page = MagicMock()
    page.url = (
        "https://studio.youtube.com/channel/test"
        if authenticated
        else "https://accounts.google.com/ServiceLogin"
    )
    page.locator.return_value.count.return_value = 1 if authenticated else 0
    context = MagicMock()
    context.pages = [page]
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context.return_value = context
    starter = MagicMock()
    starter.start.return_value = playwright
    return starter, playwright, context, page


def test_opens_authenticated_persistent_session_and_saves_trace(tmp_path: Path) -> None:
    starter, playwright, context, page = _playwright_fixture()
    manager = StudioBrowserManager(_browser_config(tmp_path), _diagnostics(tmp_path))
    with patch("youtube_copyright.browser_session.sync_playwright", return_value=starter):
        session = manager.open("run-1", video_id="abc")
        trace_path = session.trace_path
        session.close(success=True)

    playwright.chromium.launch_persistent_context.assert_called_once()
    assert (
        playwright.chromium.launch_persistent_context.call_args.kwargs["channel"]
        == "chrome"
    )
    context.storage_state.assert_called()
    context.tracing.stop.assert_called_once_with(path=str(trace_path))
    page.goto.assert_called_once()
    playwright.stop.assert_called_once()


def test_manual_login_uses_regular_browser_then_verifies_profile(
    tmp_path: Path,
) -> None:
    manager = StudioBrowserManager(_browser_config(tmp_path), _diagnostics(tmp_path))
    executable = tmp_path / "chrome.exe"
    executable.touch()
    verified_session = MagicMock()

    with (
        patch.object(manager, "_resolve_browser_executable", return_value=executable),
        patch("youtube_copyright.browser_session.subprocess.Popen") as open_browser,
        patch("builtins.input", return_value="") as confirm_closed,
        patch.object(manager, "open", return_value=verified_session) as open_session,
    ):
        open_browser.return_value.wait.return_value = 0
        manager.login()

    command = open_browser.call_args.args[0]
    assert command[0] == str(executable)
    assert any(argument.startswith("--user-data-dir=") for argument in command)
    assert "--remote-debugging-port" not in " ".join(command)
    assert command[-1] == "https://studio.youtube.com/"
    confirm_closed.assert_called_once()
    open_session.assert_called_once_with("interactive-login-verification")
    verified_session.__enter__.assert_called_once()
    verified_session.__exit__.assert_called_once()


def test_manual_login_retries_until_chrome_profile_is_released(
    tmp_path: Path,
) -> None:
    manager = StudioBrowserManager(_browser_config(tmp_path), _diagnostics(tmp_path))
    executable = tmp_path / "chrome.exe"
    executable.touch()
    verified_session = MagicMock()
    busy = RuntimeError("Opening in existing browser session")

    with (
        patch.object(manager, "_resolve_browser_executable", return_value=executable),
        patch("youtube_copyright.browser_session.subprocess.Popen") as open_browser,
        patch("builtins.input", return_value=""),
        patch("youtube_copyright.browser_session.time.sleep") as sleep,
        patch.object(
            manager, "open", side_effect=[busy, verified_session]
        ) as open_session,
    ):
        open_browser.return_value.wait.return_value = 0
        manager.login()

    assert open_session.call_count == 2
    sleep.assert_called_once_with(1)


def test_refuses_unattended_login_when_session_expired(tmp_path: Path) -> None:
    starter, playwright, context, page = _playwright_fixture(authenticated=False)
    manager = StudioBrowserManager(_browser_config(tmp_path), _diagnostics(tmp_path))
    with (
        patch("youtube_copyright.browser_session.sync_playwright", return_value=starter),
        pytest.raises(StudioAuthRequired, match="--login"),
    ):
        manager.open("run-2")
    context.close.assert_called_once()
    playwright.stop.assert_called_once()


def test_sigint_skips_blocking_playwright_cleanup(tmp_path: Path) -> None:
    starter, playwright, context, page = _playwright_fixture()
    manager = StudioBrowserManager(_browser_config(tmp_path), _diagnostics(tmp_path))
    with patch("youtube_copyright.browser_session.sync_playwright", return_value=starter):
        session = manager.open("run-interrupted", video_id="abc")
        context.reset_mock()
        playwright.stop.reset_mock()
        session.__exit__(KeyboardInterrupt, KeyboardInterrupt(), None)

    context.storage_state.assert_not_called()
    context.tracing.stop.assert_not_called()
    context.close.assert_not_called()
    playwright.stop.assert_not_called()


def test_pruning_removes_only_marked_guard_run_directories(tmp_path: Path) -> None:
    config = _diagnostics(tmp_path)
    marked = DiagnosticRun(config, "old-run").directory
    unmarked = config.directory / "unrelated"
    unmarked.mkdir()
    os.utime(marked, (0, 0))
    os.utime(unmarked, (0, 0))
    removed = prune_diagnostics(config)
    assert marked in removed
    assert not marked.exists()
    assert unmarked.exists()
