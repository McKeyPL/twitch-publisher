"""Dedicated persistent Chromium session for YouTube Studio."""

from __future__ import annotations

import logging
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
        interactive_login: bool = False,
    ) -> StudioBrowserSession:
        if sync_playwright is None:
            raise StudioBrowserError(
                "Playwright is unavailable; install requirements and Chromium"
            )
        diagnostic = DiagnosticRun(self.diagnostics_config, run_id, video_id)
        playwright = sync_playwright().start()
        context = None
        trace_path: Path | None = None
        try:
            self.browser_config.user_data_directory.mkdir(parents=True, exist_ok=True)
            self.browser_config.storage_state_file.parent.mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.browser_config.user_data_directory),
                headless=self.browser_config.headless,
                locale=self.browser_config.locale,
                viewport={"width": 1440, "height": 1000},
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
            if not self.is_authenticated(page):
                if not interactive_login:
                    raise StudioAuthRequired(
                        "YouTube Studio authentication is required; run copyright_guard.py --login"
                    )
                if self.browser_config.headless:
                    raise StudioAuthRequired("Interactive Studio login requires headless=false")
                print(
                    "[youtube-studio] Sign in in the open browser, then press Enter"
                )
                input()
                page.goto(STUDIO_HOME, wait_until="domcontentloaded")
                if not self.is_authenticated(page):
                    raise StudioAuthRequired("YouTube Studio is still not authenticated")
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
                lambda request: logger.error(
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
