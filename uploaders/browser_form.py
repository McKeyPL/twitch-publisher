"""Male, testowalne narzedzia dla formularzy Playwright."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


logger = logging.getLogger(__name__)
UPLOAD_TIMEOUT_MS = 12 * 60 * 60 * 1000
HEARTBEAT_INTERVAL_MS = 30 * 1000
CANCEL_POLL_INTERVAL_MS = 1000

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover - Playwright jest opcjonalny dla testow
    PlaywrightTimeoutError = TimeoutError


class BrowserUploadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retriable: bool = True,
        manual_review_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.retriable = retriable
        self.manual_review_required = manual_review_required


def should_retry_browser_error(exc: Exception) -> bool:
    """Nie ponawia operacji po celowym/fatalnym zamknieciu przegladarki."""
    if isinstance(exc, BrowserUploadError):
        return exc.retriable
    class_name = type(exc).__name__.casefold()
    message = str(exc).casefold()
    target_closed = (
        "targetclosed" in class_name
        or "target page, context or browser has been closed" in message
        or "browser has been closed" in message
    )
    return not target_closed


def unique_visible_locator(page: Any, selectors: Iterable[str], field_name: str) -> Any:
    """Zwraca pierwsza jednoznaczna widoczna kontrolke z listy stabilnych selektorow."""
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() == 1 and locator.is_visible():
            return locator
    raise BrowserUploadError(
        f"Nie znaleziono jednoznacznego pola {field_name}; formularz mogl sie zmienic",
        retriable=False,
    )


def unique_locator(page: Any, selectors: Iterable[str], field_name: str) -> Any:
    """Jak unique_visible_locator, ale dopuszcza ukryty input typu file."""
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() == 1:
            return locator
    raise BrowserUploadError(
        f"Nie znaleziono jednoznacznego pola {field_name}; formularz mogl sie zmienic",
        retriable=False,
    )


def optional_visible_locator(page: Any, selectors: Iterable[str]) -> Any | None:
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() == 1 and locator.is_visible():
            return locator
    return None


def visible_error_text(page: Any, selectors: Iterable[str]) -> str | None:
    """Zwraca pierwszy widoczny komunikat bledu formularza."""
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                text = (candidate.text_content() or "").strip()
                if text:
                    return text
    return None


def capture_browser_debug(
    page: Any,
    *,
    platform: str,
    debug_directory: Path,
    stage: str,
    take_screenshot: bool = True,
) -> Path | None:
    """Loguje stan DOM i opcjonalnie zapisuje screenshot bez HTML/cookies."""
    try:
        parsed = urlparse(str(page.url))
        safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        title = page.title()
        file_inputs = page.locator("input[type='file']").evaluate_all(
            """elements => elements.map(element => ({
                id: element.id || null,
                name: element.name || null,
                accept: element.accept || null,
                disabled: element.disabled,
                files: Array.from(element.files || []).map(file => ({
                    name: file.name,
                    size: file.size,
                    type: file.type || null
                }))
            }))"""
        )
        diagnostics: list[dict[str, Any]] = []
        selectors = (
            "#upload1",
            "#nazwa_wyswietlana",
            ".qq-upload-list",
            ".qq-upload-status-text",
            ".qq-upload-size",
            ".progress",
            "[role='progressbar']",
            ".alert-danger",
            ".upload-error",
        )
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 3)):
                item = locator.nth(index)
                diagnostics.append(
                    {
                        "selector": selector,
                        "visible": item.is_visible(),
                        "text": (item.text_content() or "").strip()[:500],
                    }
                )
        logger.info(
            "%s debug[%s]: url=%s title=%r file_inputs=%s dom=%s",
            platform,
            stage,
            safe_url,
            title,
            file_inputs,
            diagnostics,
        )
        if not take_screenshot:
            return None
        debug_directory.mkdir(parents=True, exist_ok=True)
        safe_stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", stage)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        screenshot_path = debug_directory / f"{platform}_{timestamp}_{safe_stage}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info("%s: zapisano screenshot diagnostyczny: %s", platform, screenshot_path)
        return screenshot_path
    except Exception:
        logger.warning("%s: nie udalo sie zebrac diagnostyki przegladarki", platform, exc_info=True)
        return None


def validate_upload_files(video_path: Path, srt_path: Path | None) -> None:
    if not video_path.is_file():
        raise BrowserUploadError(f"Plik wideo nie istnieje: {video_path}", retriable=False)
    if srt_path is not None and not srt_path.is_file():
        raise BrowserUploadError(f"Plik SRT nie istnieje: {srt_path}", retriable=False)


def report_manual_captions(platform: str, srt_path: Path | None) -> None:
    if srt_path is None or srt_path.stat().st_size == 0:
        return
    logger.warning(
        "%s: formularz nie ma pola SRT; napisy wymagaja manualnego dodania: %s",
        platform,
        srt_path,
    )


def wait_for_visible_with_heartbeat(
    locator: Any,
    *,
    platform: str,
    field_name: str,
    timeout_ms: int = UPLOAD_TIMEOUT_MS,
    heartbeat_interval_ms: int = HEARTBEAT_INTERVAL_MS,
    failure_probe: Callable[[], str | None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    heartbeat_probe: Callable[[], None] | None = None,
) -> None:
    """Czeka na kontrolke, okresowo potwierdzajac ze proces nadal pracuje."""
    if timeout_ms <= 0 or heartbeat_interval_ms <= 0:
        raise ValueError("timeout i interwal heartbeat musza byc dodatnie")

    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    next_heartbeat = started + heartbeat_interval_ms / 1000
    while True:
        if cancel_check is not None:
            cancel_check()
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        slice_ms = min(CANCEL_POLL_INTERVAL_MS, remaining_ms)
        try:
            locator.wait_for(state="visible", timeout=slice_ms)
            logger.info("%s: pole %s jest gotowe", platform, field_name)
            return
        except PlaywrightTimeoutError as exc:
            if cancel_check is not None:
                cancel_check()
            elapsed = time.monotonic() - started
            failure = failure_probe() if failure_probe is not None else None
            if failure:
                raise BrowserUploadError(
                    f"Formularz zglosil blad podczas oczekiwania na {field_name}: "
                    f"{failure}",
                    retriable=False,
                ) from exc
            if time.monotonic() >= deadline:
                raise BrowserUploadError(
                    f"Przekroczono limit oczekiwania na {field_name} "
                    f"({timeout_ms / 1000:.0f} s)",
                    retriable=True,
                ) from exc
            if time.monotonic() >= next_heartbeat:
                logger.info(
                    "%s: nadal oczekuje na %s (%.0f s); "
                    "sam heartbeat nie potwierdza transferu danych",
                    platform,
                    field_name,
                    elapsed,
                )
                if heartbeat_probe is not None:
                    heartbeat_probe()
                next_heartbeat = time.monotonic() + heartbeat_interval_ms / 1000


def video_id_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or url


def wait_for_video_url(
    page: Any,
    pattern: str,
    *,
    platform: str = "formularz",
    cancel_check: Callable[[], None] | None = None,
    heartbeat_probe: Callable[[], None] | None = None,
) -> str:
    started = time.monotonic()
    deadline = started + UPLOAD_TIMEOUT_MS / 1000
    next_heartbeat = started + HEARTBEAT_INTERVAL_MS / 1000
    compiled_pattern = re.compile(pattern)
    while True:
        if cancel_check is not None:
            cancel_check()
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        try:
            page.wait_for_url(
                compiled_pattern,
                timeout=min(CANCEL_POLL_INTERVAL_MS, remaining_ms),
            )
            break
        except PlaywrightTimeoutError as exc:
            if cancel_check is not None:
                cancel_check()
            if time.monotonic() >= deadline:
                raise BrowserUploadError(
                    "Nie potwierdzono zakonczenia uploadu po wyslaniu formularza. "
                    "Nie ponawiam automatycznie, aby nie utworzyc duplikatu.",
                    retriable=False,
                    manual_review_required=True,
                ) from exc
            if time.monotonic() >= next_heartbeat:
                logger.info(
                    "%s: nadal oczekuje na potwierdzenie publikacji (%.0f s)",
                    platform,
                    time.monotonic() - started,
                )
                if heartbeat_probe is not None:
                    heartbeat_probe()
                next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL_MS / 1000
        except Exception as exc:
            raise BrowserUploadError(
                "Przegladarka przerwala oczekiwanie na potwierdzenie publikacji. "
                "Nie ponawiam automatycznie, aby nie utworzyc duplikatu.",
                retriable=False,
                manual_review_required=True,
            ) from exc
    return page.url
