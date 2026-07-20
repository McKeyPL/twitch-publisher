from __future__ import annotations

import logging

import pytest

from uploaders import browser_form


class LocatorReadyAfterOneHeartbeat:
    def __init__(self) -> None:
        self.calls = 0

    def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "visible"
        assert timeout > 0
        self.calls += 1
        if self.calls == 1:
            raise browser_form.PlaywrightTimeoutError("jeszcze trwa")


class PageReadyAfterOneHeartbeat:
    url = "https://www.cda.pl/video/abc123"

    def __init__(self) -> None:
        self.calls = 0

    def wait_for_url(self, pattern, *, timeout: int) -> None:
        assert pattern.search(self.url)
        assert timeout > 0
        self.calls += 1
        if self.calls == 1:
            raise browser_form.PlaywrightTimeoutError("jeszcze trwa")


def test_visible_wait_logs_heartbeat_and_then_returns(caplog) -> None:
    locator = LocatorReadyAfterOneHeartbeat()

    with caplog.at_level(logging.INFO):
        browser_form.wait_for_visible_with_heartbeat(
            locator,
            platform="cda",
            field_name="formularz metadanych",
            timeout_ms=10_000,
            heartbeat_interval_ms=1,
        )

    assert locator.calls == 2
    assert "nadal oczekuje na formularz metadanych" in caplog.text
    assert "pole formularz metadanych jest gotowe" in caplog.text


def test_video_url_wait_logs_heartbeat_and_returns_url(caplog, monkeypatch) -> None:
    page = PageReadyAfterOneHeartbeat()
    monkeypatch.setattr(browser_form, "HEARTBEAT_INTERVAL_MS", 1)

    with caplog.at_level(logging.INFO):
        result = browser_form.wait_for_video_url(page, r"cda\.pl/video/")

    assert result == page.url
    assert page.calls == 2
    assert "nadal oczekuje na potwierdzenie publikacji" in caplog.text


def test_visible_wait_rejects_invalid_intervals() -> None:
    with pytest.raises(ValueError, match="musza byc dodatnie"):
        browser_form.wait_for_visible_with_heartbeat(
            object(),
            platform="cda",
            field_name="formularz",
            timeout_ms=0,
        )
