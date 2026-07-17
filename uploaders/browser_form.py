"""Male, testowalne narzedzia dla formularzy Playwright."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


logger = logging.getLogger(__name__)
UPLOAD_TIMEOUT_MS = 12 * 60 * 60 * 1000


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


def video_id_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or url


def wait_for_video_url(page: Any, pattern: str) -> str:
    try:
        page.wait_for_url(re.compile(pattern), timeout=UPLOAD_TIMEOUT_MS)
    except Exception as exc:
        raise BrowserUploadError(
            "Nie potwierdzono zakonczenia uploadu po wyslaniu formularza. "
            "Nie ponawiam automatycznie, aby nie utworzyc duplikatu.",
            retriable=False,
        ) from exc
    return page.url
