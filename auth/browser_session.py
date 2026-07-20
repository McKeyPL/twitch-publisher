"""Playwright session: storage state, Firefox cookies, then manual login."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from config import BrowserConfig, BrowserPlatformConfig

try:  # Keep the import optional for unit tests.
    import browser_cookie3
except ImportError:  # pragma: no cover
    browser_cookie3 = None  # type: ignore[assignment]

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)
AuthCheck = Callable[[Any], bool]


class BrowserSessionError(RuntimeError):
    """An active platform session could not be obtained."""


@dataclass(slots=True)
class AuthenticatedBrowserSession:
    page: Any
    context: Any
    browser: Any
    playwright: Any
    trace_path: Path | None = None

    def close(self, *, save_trace: bool = False) -> None:
        if self.trace_path is not None:
            try:
                if save_trace:
                    self.trace_path.parent.mkdir(parents=True, exist_ok=True)
                    logger.info("Saving Playwright trace after an error: %s", self.trace_path)
                    self.context.tracing.stop(path=str(self.trace_path))
                    logger.info("Saved Playwright trace: %s", self.trace_path)
                else:
                    # Packaging a long trace after a multi-gigabyte upload can block
                    # shutdown for minutes. A trace is unnecessary after success or
                    # cancellation; screenshots and logs remain on disk.
                    self.context.tracing.stop()
                    logger.info("Stopped Playwright tracing without saving an archive")
            except Exception:
                logger.warning("Could not stop Playwright tracing", exc_info=True)
        for resource in (self.context, self.browser):
            try:
                resource.close()
            except Exception:  # pragma: no cover - emergency cleanup
                logger.debug("Error while closing a Playwright resource", exc_info=True)
        try:
            self.playwright.stop()
        except Exception:  # pragma: no cover
            logger.debug("Error while stopping Playwright", exc_info=True)
        else:
            logger.info("Closed Playwright session")

    def __enter__(self) -> "AuthenticatedBrowserSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        interrupted = exc_type is not None and issubclass(
            exc_type, (KeyboardInterrupt, SystemExit)
        )
        self.close(save_trace=exc_type is not None and not interrupted)


class BrowserSessionManager:
    """Open a session using storage state, Firefox cookies, then interactive login."""

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config

    def open(
        self,
        platform_name: str,
        platform_config: BrowserPlatformConfig,
        is_authenticated: AuthCheck,
    ) -> AuthenticatedBrowserSession:
        if sync_playwright is None:
            raise BrowserSessionError(
                "Playwright is unavailable. Install requirements.txt and run "
                "playwright install firefox"
            )

        playwright = sync_playwright().start()
        browser = None
        try:
            browser = playwright.firefox.launch(
                headless=False if self.config.debug else self.config.headless
            )
            session = self._try_storage_state(
                playwright, browser, platform_name, platform_config, is_authenticated
            )
            if session is not None:
                return session
            session = self._try_firefox_cookies(
                playwright, browser, platform_name, platform_config, is_authenticated
            )
            if session is not None:
                return session

            browser.close()
            browser = playwright.firefox.launch(
                headless=self.config.interactive_login_headless
            )
            context = browser.new_context()
            trace_path = self._prepare_context(context, platform_name)
            page = context.new_page()
            self._prepare_page(page, platform_name)
            page.goto(platform_config.upload_url, wait_until="domcontentloaded")
            print(
                f"[{platform_name}] Sign in manually in the open window, "
                "then press Enter"
            )
            input()
            page.goto(platform_config.upload_url, wait_until="domcontentloaded")
            if not is_authenticated(page):
                context.close()
                raise BrowserSessionError(
                    f"{platform_name}: no active session after manual confirmation"
                )
            self._save_state(context, platform_config.storage_state_file)
            return AuthenticatedBrowserSession(
                page, context, browser, playwright, trace_path
            )
        except Exception:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            playwright.stop()
            raise

    def _try_storage_state(
        self,
        playwright: Any,
        browser: Any,
        platform_name: str,
        platform_config: BrowserPlatformConfig,
        is_authenticated: AuthCheck,
    ) -> AuthenticatedBrowserSession | None:
        state_path = platform_config.storage_state_file
        if not state_path.is_file():
            return None
        context = None
        try:
            context = browser.new_context(storage_state=str(state_path))
            trace_path = self._prepare_context(context, platform_name)
            page = context.new_page()
            self._prepare_page(page, platform_name)
            page.goto(platform_config.upload_url, wait_until="domcontentloaded")
            if is_authenticated(page):
                logger.info("%s: using saved storage_state", platform_name)
                return AuthenticatedBrowserSession(
                    page, context, browser, playwright, trace_path
                )
            logger.info("%s: saved storage_state has expired", platform_name)
        except Exception as exc:
            logger.warning("%s: cannot use storage_state: %s", platform_name, exc)
        if context is not None:
            context.close()
        return None

    def _try_firefox_cookies(
        self,
        playwright: Any,
        browser: Any,
        platform_name: str,
        platform_config: BrowserPlatformConfig,
        is_authenticated: AuthCheck,
    ) -> AuthenticatedBrowserSession | None:
        if browser_cookie3 is None:
            logger.warning("%s: browser_cookie3 is not installed", platform_name)
            return None
        domain = (urlparse(platform_config.upload_url).hostname or "").removeprefix("www.")
        kwargs: dict[str, Any] = {"domain_name": domain}
        cookie_file = self._firefox_cookie_file()
        if cookie_file is not None:
            kwargs["cookie_file"] = str(cookie_file)
        try:
            jar = browser_cookie3.firefox(**kwargs)
            cookies = [_to_playwright_cookie(cookie) for cookie in jar]
        except (PermissionError, sqlite3.OperationalError, OSError) as exc:
            logger.warning(
                "%s: Firefox cookie database is locked (%s). Close Firefox; "
                "falling back to manual login.", platform_name, exc
            )
            return None
        except Exception as exc:
            logger.warning("%s: could not read cookies: %s", platform_name, exc)
            return None
        if not cookies:
            return None

        context = browser.new_context()
        trace_path = self._prepare_context(context, platform_name)
        try:
            context.add_cookies(cookies)
            page = context.new_page()
            self._prepare_page(page, platform_name)
            page.goto(platform_config.upload_url, wait_until="domcontentloaded")
            if not is_authenticated(page):
                context.close()
                return None
            self._save_state(context, platform_config.storage_state_file)
            logger.info("%s: saved session imported from Firefox", platform_name)
            return AuthenticatedBrowserSession(
                page, context, browser, playwright, trace_path
            )
        except Exception as exc:
            logger.warning("%s: cannot inject cookies: %s", platform_name, exc)
            context.close()
            return None

    def _firefox_cookie_file(self) -> Path | None:
        profile = self.config.firefox_profile_path
        if profile is None:
            return None
        return profile / "cookies.sqlite" if profile.is_dir() else profile

    def _prepare_context(self, context: Any, platform_name: str) -> Path | None:
        if not self.config.debug:
            return None
        debug_directory = self.config.debug_directory
        debug_directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        trace_path = debug_directory / f"{platform_name}_{timestamp}_trace.zip"
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        logger.info(
            "%s: Playwright tracing enabled; the archive is saved only on error -> %s",
            platform_name,
            trace_path,
        )
        return trace_path

    def _prepare_page(self, page: Any, platform_name: str) -> None:
        if not self.config.debug:
            return

        def safe_url(value: str) -> str:
            parsed = urlparse(value)
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

        page.on(
            "console",
            lambda message: logger.info(
                "%s browser console[%s]: %s",
                platform_name,
                message.type,
                message.text[:1000],
            ),
        )
        page.on(
            "pageerror",
            lambda error: logger.error("%s browser pageerror: %s", platform_name, error),
        )
        page.on(
            "requestfailed",
            lambda request: logger.error(
                "%s request failed: %s %s (%s)",
                platform_name,
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
                    platform_name,
                    response.status,
                    safe_url(response.url),
                )
                if response.status >= 400
                else None
            ),
        )

    @staticmethod
    def _save_state(context: Any, state_path: Path) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(state_path))


def _to_playwright_cookie(cookie: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path or "/",
        "secure": bool(cookie.secure),
    }
    if cookie.expires and cookie.expires > 0:
        result["expires"] = float(cookie.expires)
    rest = getattr(cookie, "_rest", {}) or {}
    lowered_keys = {str(key).lower() for key in rest}
    result["httpOnly"] = "httponly" in lowered_keys
    same_site = str(rest.get("SameSite", rest.get("samesite", ""))).lower()
    mapping = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}
    if same_site in mapping:
        result["sameSite"] = mapping[same_site]
    return result
