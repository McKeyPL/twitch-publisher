from __future__ import annotations

from http.cookiejar import Cookie
from pathlib import Path

from auth.browser_session import AuthenticatedBrowserSession, _to_playwright_cookie


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
        self.stop_calls = []

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
