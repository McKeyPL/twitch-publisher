"""Dedicated persistent Chromium session for YouTube Studio."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import CopyrightBrowserConfig, CopyrightDiagnosticsConfig

from .diagnostics import DiagnosticRun, safe_url


try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)
STUDIO_HOME = "https://studio.youtube.com/"


def _request_failure_level(failure: str | None) -> int:
    # Chromium aborts obsolete background requests while Studio changes routes.
    # These are expected navigation noise, unlike DNS/TLS/connection failures.
    return logging.DEBUG if "ERR_ABORTED" in (failure or "") else logging.ERROR


class StudioAuthRequired(RuntimeError):
    pass


class StudioBrowserError(RuntimeError):
    pass


@dataclass(slots=True)
class StudioBrowserSession:
    page: Any
    context: Any
    playwright: Any
    config: CopyrightBrowserConfig
    diagnostic: DiagnosticRun
    trace_path: Path | None
    _closed: bool = False

    def close(self, *, success: bool) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.context.storage_state(path=str(self.config.storage_state_file))
        except Exception:
            logger.warning("Could not save YouTube Studio storage state", exc_info=True)
        if self.trace_path is not None:
            save_trace = self.config.trace_mode == "always" or (
                self.config.trace_mode == "on_error" and not success
            )
            try:
                if save_trace:
                    self.context.tracing.stop(path=str(self.trace_path))
                    logger.info("Saved YouTube Studio trace: %s", self.trace_path)
                else:
                    self.context.tracing.stop()
            except Exception:
                logger.warning("Could not stop YouTube Studio trace", exc_info=True)
        try:
            self.context.close()
        finally:
            self.playwright.stop()

    def __enter__(self) -> "StudioBrowserSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is not None and issubclass(exc_type, KeyboardInterrupt):
            # Never start another synchronous Playwright operation while one was
            # interrupted. It can wait for the original pending locator for many
            # minutes. The process exits immediately after persisting UNCERTAIN.
            self._closed = True
            logger.info(
                "Skipping Playwright cleanup after SIGINT; the browser transport "
                "will close with the guard process"
            )
            return
        self.close(success=exc_type is None)


class StudioBrowserManager:
    def __init__(
        self,
        browser_config: CopyrightBrowserConfig,
        diagnostics_config: CopyrightDiagnosticsConfig,
    ) -> None:
        self.browser_config = browser_config
        self.diagnostics_config = diagnostics_config

    def open(
        self,
        run_id: str,
        *,
        video_id: str | None = None,
    ) -> StudioBrowserSession:
        if sync_playwright is None:
            raise StudioBrowserError(
                "Playwright is unavailable; install requirements and Chromium"
            )
        diagnostic = DiagnosticRun(
            self.diagnostics_config,
            run_id,
            video_id,
            screenshots_enabled=self.browser_config.screenshots,
        )
        playwright = sync_playwright().start()
        context = None
        trace_path: Path | None = None
        try:
            self.browser_config.user_data_directory.mkdir(parents=True, exist_ok=True)
            self.browser_config.storage_state_file.parent.mkdir(parents=True, exist_ok=True)
            launch_options: dict[str, Any] = {
                "headless": self.browser_config.headless,
                "locale": self.browser_config.locale,
                "viewport": {"width": 1440, "height": 1000},
            }
            if self.browser_config.executable_path is not None:
                launch_options["executable_path"] = str(
                    self.browser_config.executable_path
                )
            else:
                launch_options["channel"] = self.browser_config.channel
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.browser_config.user_data_directory),
                **launch_options,
            )
            context.set_default_timeout(
                self.browser_config.action_timeout_seconds * 1000
            )
            context.set_default_navigation_timeout(
                self.browser_config.navigation_timeout_seconds * 1000
            )
            if self.browser_config.trace_mode != "off":
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                trace_path = diagnostic.path("trace", ".zip")
            page = context.pages[0] if context.pages else context.new_page()
            self._attach_logging(page, run_id, video_id)
            page.goto(STUDIO_HOME, wait_until="domcontentloaded")
            if "accounts.google.com" not in page.url:
                try:
                    page.locator("ytcp-app").wait_for(state="attached", timeout=15_000)
                except Exception:
                    logger.debug("Studio application shell did not attach within 15 seconds")
            if not self.is_authenticated(page):
                raise StudioAuthRequired(
                    "YouTube Studio authentication is required; run copyright_guard.py --login"
                )
            context.storage_state(path=str(self.browser_config.storage_state_file))
            return StudioBrowserSession(
                page=page,
                context=context,
                playwright=playwright,
                config=self.browser_config,
                diagnostic=diagnostic,
                trace_path=trace_path,
            )
        except Exception:
            if context is not None:
                try:
                    if trace_path is not None:
                        context.tracing.stop(path=str(trace_path))
                        logger.info("Saved failed YouTube Studio trace: %s", trace_path)
                except Exception:
                    logger.warning("Could not save failed Studio trace", exc_info=True)
                try:
                    context.close()
                except Exception:
                    pass
            playwright.stop()
            raise

    def login(self, run_id: str = "interactive-login") -> None:
        """Create the Studio profile in a normal browser, then verify it.

        Google explicitly blocks some sign-ins performed in browsers controlled by
        automation. The login browser is therefore started directly, without
        Playwright or a remote-debugging connection. Playwright only opens the same
        dedicated profile after the user has closed the regular browser.
        """

        if self.browser_config.headless:
            raise StudioAuthRequired("Interactive Studio login requires headless=false")
        executable = self._resolve_browser_executable()
        profile = self.browser_config.user_data_directory.resolve()
        profile.mkdir(parents=True, exist_ok=True)
        command = [
            str(executable),
            f"--user-data-dir={profile}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--new-window",
            STUDIO_HOME,
        ]
        logger.info(
            "Opening a regular %s browser for manual YouTube Studio login: %s",
            self.browser_config.channel,
            executable,
        )
        print(
            "[youtube-studio] Sign in to YouTube Studio in the regular browser.\n"
            "[youtube-studio] When Studio has loaded, CLOSE THAT BROWSER WINDOW "
            "completely, then return here and press Enter."
        )
        try:
            process = subprocess.Popen(command)
        except OSError as exc:
            raise StudioBrowserError(
                f"Could not start the configured login browser: {executable}: {exc}"
            ) from exc
        input("[youtube-studio] Press Enter after closing the dedicated browser: ")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.info(
                "The browser launcher process is still alive; waiting for the profile "
                "lock to be released"
            )

        for attempt in range(1, 16):
            try:
                with self.open(f"{run_id}-verification"):
                    logger.info("The manually created YouTube Studio session is valid")
                return
            except StudioAuthRequired as exc:
                raise StudioAuthRequired(
                    "The regular browser closed, but the dedicated profile is not "
                    "signed in to YouTube Studio. Run --login again and wait until "
                    "Studio itself has loaded before closing the window."
                ) from exc
            except Exception as exc:
                if self._profile_is_busy(exc) and attempt < 15:
                    logger.info(
                        "The dedicated Chrome profile is still closing; verification "
                        "retry %d/15",
                        attempt + 1,
                    )
                    time.sleep(1)
                    continue
                if self._profile_is_busy(exc):
                    raise StudioBrowserError(
                        "The dedicated YouTube Studio browser profile is still in use. "
                        "Close every Chrome/Edge window opened for the guard and run "
                        "--login again."
                    ) from exc
                raise StudioBrowserError(
                    f"Could not verify the YouTube Studio browser session: {exc}"
                ) from exc

    @staticmethod
    def _profile_is_busy(error: Exception) -> bool:
        message = str(error).casefold()
        return any(
            marker in message
            for marker in (
                "opening in existing browser session",
                "profile is already in use",
                "user data directory is already in use",
            )
        )

    def _resolve_browser_executable(self) -> Path:
        configured = self.browser_config.executable_path
        if configured is not None:
            candidate = configured.expanduser().resolve()
            if candidate.is_file():
                return candidate
            raise StudioBrowserError(
                "youtube_copyright.browser.executable_path does not point to a file: "
                f"{candidate}"
            )

        channel = self.browser_config.channel
        names = (
            ("chrome.exe", "chrome", "google-chrome", "google-chrome-stable")
            if channel == "chrome"
            else ("msedge.exe", "msedge", "microsoft-edge", "microsoft-edge-stable")
        )
        candidates: list[Path] = []
        for name in names:
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        if sys.platform == "win32":
            roots = [
                os.environ.get("PROGRAMFILES"),
                os.environ.get("PROGRAMFILES(X86)"),
                os.environ.get("LOCALAPPDATA"),
            ]
            relative = (
                Path("Google/Chrome/Application/chrome.exe")
                if channel == "chrome"
                else Path("Microsoft/Edge/Application/msedge.exe")
            )
            candidates.extend(Path(root) / relative for root in roots if root)
        elif sys.platform == "darwin":
            candidates.append(
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                if channel == "chrome"
                else Path(
                    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
                )
            )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise StudioBrowserError(
            f"Could not find installed {channel}. Install it or set "
            "YOUTUBE_STUDIO_BROWSER_PATH to its executable."
        )

    @staticmethod
    def is_authenticated(page: Any) -> bool:
        if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
            return False
        if not page.url.startswith("https://studio.youtube.com"):
            return False
        return bool(
            page.locator("ytcp-app").count()
            or page.locator("button#avatar-btn").count()
            or page.locator("[aria-label*='Account']").count()
        )

    def _attach_logging(
        self, page: Any, run_id: str, video_id: str | None
    ) -> None:
        prefix = f"studio run={run_id} video={video_id or '-'}"
        if self.browser_config.console_logging:
            page.on(
                "console",
                lambda message: logger.info(
                    "%s console[%s]: %s", prefix, message.type, message.text[:2000]
                ),
            )
            page.on(
                "pageerror",
                lambda error: logger.error("%s pageerror: %s", prefix, error),
            )
        if self.browser_config.failed_request_logging:
            page.on(
                "requestfailed",
                lambda request: logger.log(
                    _request_failure_level(request.failure),
                    "%s request failed: %s %s (%s)",
                    prefix,
                    request.method,
                    safe_url(request.url),
                    request.failure,
                ),
            )
            page.on(
                "response",
                lambda response: (
                    logger.warning(
                        "%s HTTP %s: %s",
                        prefix,
                        response.status,
                        safe_url(response.url),
                    )
                    if response.status >= 400
                    else None
                ),
            )
