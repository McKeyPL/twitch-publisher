"""Male, testowalne narzedzia dla formularzy Playwright."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


logger = logging.getLogger(__name__)
UPLOAD_TIMEOUT_MS = 12 * 60 * 60 * 1000
HEARTBEAT_INTERVAL_MS = 30 * 1000

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover - Playwright jest opcjonalny dla testow
    PlaywrightTimeoutError = TimeoutError


class BrowserUploadError(RuntimeError):
    def __init__(self, message: str, *, retriable: bool = True) -> None:
        super().__init__(message)
        self.retriable = retriable


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


def optional_visible_locator(page: Any, selectors: Iterable[str]) -> Any | None:
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() == 1 and locator.is_visible():
            return locator
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
) -> None:
    """Czeka na kontrolke, okresowo potwierdzajac ze proces nadal pracuje."""
    if timeout_ms <= 0 or heartbeat_interval_ms <= 0:
        raise ValueError("timeout i interwal heartbeat musza byc dodatnie")

    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    while True:
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        slice_ms = min(heartbeat_interval_ms, remaining_ms)
        try:
            locator.wait_for(state="visible", timeout=slice_ms)
            logger.info("%s: pole %s jest gotowe", platform, field_name)
            return
        except PlaywrightTimeoutError as exc:
            elapsed = time.monotonic() - started
            if time.monotonic() >= deadline:
                raise BrowserUploadError(
                    f"Przekroczono limit oczekiwania na {field_name} "
                    f"({timeout_ms / 1000:.0f} s)",
                    retriable=True,
                ) from exc
            logger.info(
                "%s: nadal oczekuje na %s (%.0f s); upload/przetwarzanie trwa",
                platform,
                field_name,
                elapsed,
            )


def video_id_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or url


def wait_for_video_url(page: Any, pattern: str) -> str:
    started = time.monotonic()
    deadline = started + UPLOAD_TIMEOUT_MS / 1000
    compiled_pattern = re.compile(pattern)
    while True:
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        try:
            page.wait_for_url(
                compiled_pattern,
                timeout=min(HEARTBEAT_INTERVAL_MS, remaining_ms),
            )
            break
        except PlaywrightTimeoutError as exc:
            if time.monotonic() >= deadline:
                raise BrowserUploadError(
                    "Nie potwierdzono zakonczenia uploadu po wyslaniu formularza. "
                    "Nie ponawiam automatycznie, aby nie utworzyc duplikatu.",
                    retriable=False,
                ) from exc
            logger.info(
                "formularz: nadal oczekuje na potwierdzenie publikacji (%.0f s)",
                time.monotonic() - started,
            )
        except Exception as exc:
            raise BrowserUploadError(
                "Przegladarka przerwala oczekiwanie na potwierdzenie publikacji. "
                "Nie ponawiam automatycznie, aby nie utworzyc duplikatu.",
                retriable=False,
            ) from exc
    return page.url
