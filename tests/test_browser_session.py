from __future__ import annotations

from http.cookiejar import Cookie

from auth.browser_session import _to_playwright_cookie


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

