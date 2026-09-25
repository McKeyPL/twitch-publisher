from __future__ import annotations

from http.cookiejar import Cookie
from pathlib import Path

from auth.browser_session import (
    AuthenticatedBrowserSession,
    BrowserSessionManager,
    _to_playwright_cookie,
)
from config import BrowserConfig


def test_converts_firefox_cookie_to_playwright_format() -> None:
    cookie = Cookie(
        0, "session", "abc", None, False, ".rumble.com", True, True, "/", True,
        True, 2_000_000_000, False, None, None,
        {"HttpOnly": None, "SameSite": "Lax"},
    )
    converted = _to_playwright_cookie(cookie)
    assert converted["name"] == "session"
    assert converted["domain"] == ".rumble.com"
    assert converted["secure"] is True
    assert converted["httpOnly"] is True
    assert converted["sameSite"] == "Lax"


class FakeTracing:
    def __init__(self) -> None:
        self.start_calls = []
        self.stop_calls = []

    def start(self, **kwargs) -> None:
        self.start_calls.append(kwargs)

    def stop(self, **kwargs) -> None:
        self.stop_calls.append(kwargs)


class FakeResource:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeContext(FakeResource):
    def __init__(self) -> None:
        super().__init__()
        self.tracing = FakeTracing()


class FakePage:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on(self, event: str, callback) -> None:
        self.events.append(event)


class FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def make_session(tmp_path: Path):
    context = FakeContext()
    browser = FakeResource()
    playwright = FakePlaywright()
    session = AuthenticatedBrowserSession(
        object(), context, browser, playwright, tmp_path / "debug" / "trace.zip"
    )
    return session, context, browser, playwright


def test_successful_session_discards_trace_before_closing(tmp_path: Path) -> None:
    session, context, browser, playwright = make_session(tmp_path)

    with session:
        pass

    assert context.tracing.stop_calls == [{}]
    assert not session.trace_path.exists()
    assert context.closed is True
    assert browser.closed is True
    assert playwright.stopped is True


def test_failed_session_saves_trace_for_diagnostics(tmp_path: Path) -> None:
    session, context, _, _ = make_session(tmp_path)

    try:
        with session:
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert context.tracing.stop_calls == [{"path": str(session.trace_path)}]


def test_interrupted_session_does_not_package_trace(tmp_path: Path) -> None:
    session, context, _, _ = make_session(tmp_path)

    try:
        with session:
            raise KeyboardInterrupt()
    except KeyboardInterrupt:
        pass

    assert context.tracing.stop_calls == [{}]


def test_browser_debug_does_not_implicitly_start_heavy_trace(tmp_path: Path) -> None:
    manager = BrowserSessionManager(
        BrowserConfig(
            firefox_profile_path=None,
            headless=True,
            interactive_login_headless=False,
            debug=True,
            trace_enabled=False,
            debug_directory=tmp_path,
        )
    )
    context = FakeContext()

    assert manager._prepare_context(context, "cda") is None
    assert context.tracing.start_calls == []


def test_visual_browser_debug_does_not_subscribe_to_network_events(
    tmp_path: Path,
) -> None:
    manager = BrowserSessionManager(
        BrowserConfig(
            firefox_profile_path=None,
            headless=True,
            interactive_login_headless=False,
            debug=True,
            trace_enabled=False,
            debug_directory=tmp_path,
        )
    )
    page = FakePage()

    manager._prepare_page(page, "cda")

    assert page.events == ["console", "pageerror"]


def test_network_events_require_explicit_memory_debug(tmp_path: Path) -> None:
    manager = BrowserSessionManager(
        BrowserConfig(
            firefox_profile_path=None,
            headless=True,
            interactive_login_headless=False,
            debug=False,
            trace_enabled=False,
            debug_directory=tmp_path,
        ),
        network_debug=True,
    )
    page = FakePage()

    manager._prepare_page(page, "cda")

    assert page.events == ["requestfailed", "response"]


def test_explicit_browser_trace_starts_without_source_capture(tmp_path: Path) -> None:
    manager = BrowserSessionManager(
        BrowserConfig(
            firefox_profile_path=None,
            headless=True,
            interactive_login_headless=False,
            debug=True,
            trace_enabled=True,
            debug_directory=tmp_path,
        )
    )
    context = FakeContext()

    trace_path = manager._prepare_context(context, "cda")

    assert trace_path is not None
    assert context.tracing.start_calls == [
        {"screenshots": True, "snapshots": True, "sources": False}
    ]
