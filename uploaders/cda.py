"""Uploader CDA oparty o aktualny, dwustopniowy formularz Playwright."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from auth.browser_session import BrowserSessionManager
from config import BrowserConfig, BrowserPlatformConfig, RetryConfig
from uploaders.base import BaseUploader, UploadResult
from uploaders.browser_form import (
    BrowserUploadError,
    capture_browser_debug,
    optional_visible_locator,
    report_manual_captions,
    should_retry_browser_error,
    unique_locator,
    unique_visible_locator,
    validate_upload_files,
    video_id_from_url,
    visible_error_text,
    wait_for_video_url,
    wait_for_visible_with_heartbeat,
)


logger = logging.getLogger(__name__)


class CDAUploader(BaseUploader):
    def __init__(
        self,
        config: BrowserPlatformConfig,
        browser_config: BrowserConfig,
        retry_config: RetryConfig,
        *,
        cancel_event: threading.Event | None = None,
        session_factory: Callable[[BrowserConfig], BrowserSessionManager] = BrowserSessionManager,
    ) -> None:
        super().__init__(retry_config, cancel_event)
        self.config = config
        self.browser_config = browser_config
        self._session_manager = session_factory(browser_config)
        self._last_debug_screenshot = 0.0

    def _debug_snapshot(self, page: object, stage: str, *, force: bool = False) -> None:
        if not self.browser_config.debug:
            return
        now = time.monotonic()
        take_screenshot = force or (
            now - self._last_debug_screenshot
            >= self.browser_config.debug_screenshot_interval_seconds
        )
        capture_browser_debug(
            page,
            platform="cda",
            debug_directory=self.browser_config.debug_directory,
            stage=stage,
            take_screenshot=take_screenshot,
        )
        if take_screenshot:
            self._last_debug_screenshot = now

    @property
    def platform_name(self) -> str:
        return "cda"

    @staticmethod
    def _is_authenticated(page: object) -> bool:
        url = str(getattr(page, "url", "")).lower()
        if "/login" in url or url.rstrip("/") == "https://www.cda.pl":
            return False
        locator = getattr(page, "locator")
        return locator("#js-upload-files").count() == 1 or locator("#nazwa_wyswietlana").count() == 1

    def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str],
        srt_path: Path | None = None,
    ) -> UploadResult:
        try:
            validate_upload_files(video_path, srt_path)
            return self._with_retry(
                lambda: self._upload_once(video_path, title, description, tags, srt_path),
                operation_name=f"upload {video_path.name}",
                should_retry=should_retry_browser_error,
            )
        except Exception as exc:
            logger.error("cda: upload %s nie powiodl sie: %s", video_path, exc)
            return UploadResult(
                success=False,
                error_message=str(exc),
                retry_allowed=not getattr(exc, "manual_review_required", False),
            )

    def _upload_once(
        self,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str],
        srt_path: Path | None,
    ) -> UploadResult:
        with self._session_manager.open("cda", self.config, self._is_authenticated) as session:
            page = session.page
            self._raise_if_cancelled()
            self._debug_snapshot(page, "session_ready", force=True)
            size_gib = video_path.stat().st_size / (1024 ** 3)
            logger.info(
                "cda: sesja gotowa (%s); wskazuje plik %.2f GiB: %s",
                page.url,
                size_gib,
                video_path,
            )
            file_input = unique_locator(
                page,
                (
                    "#js-upload-files",
                    "#upload1 input[type='file']",
                    "input[type='file']:not(#miniatura):not([name='miniatura'])",
                ),
                "pliku wideo",
            )
            file_input.set_input_files(str(video_path), timeout=60_000)
            self._raise_if_cancelled()
            self._debug_snapshot(page, "file_selected", force=True)
            logger.info(
                "cda: plik przekazany formularzowi; oczekuje na zakonczenie "
                "wysylania i formularz metadanych"
            )

            title_input = page.locator("#nazwa_wyswietlana")
            wait_for_visible_with_heartbeat(
                title_input,
                platform="cda",
                field_name="formularz metadanych",
                failure_probe=lambda: visible_error_text(
                    page,
                    (
                        "#upload1 .error",
                        ".qq-upload-fail .qq-upload-status-text",
                        ".alert-danger",
                        ".upload-error",
                    ),
                ),
                cancel_check=self._raise_if_cancelled,
                heartbeat_probe=lambda: self._debug_snapshot(
                    page, "waiting_for_metadata"
                ),
            )
            title_input.fill(title)
            unique_visible_locator(page, ("textarea[name='opis']",), "opisu").fill(description)
            tags_input = optional_visible_locator(
                page,
                (
                    "#tags_tag",  # pole tworzone przez widget jQuery tagsInput
                    "input[name='tagi']:not([type='hidden'])",
                    "#tags:not([type='hidden'])",
                ),
            )
            if tags_input is not None:
                tags_input.fill(", ".join(tags))
            elif tags:
                hidden_tags = page.locator("input[name='tagi'][type='hidden'], #tags[type='hidden']")
                if hidden_tags.count() == 1:
                    hidden_tags.evaluate(
                        """(element, value) => {
                            element.value = value;
                            element.dispatchEvent(new Event('input', {bubbles: true}));
                            element.dispatchEvent(new Event('change', {bubbles: true}));
                        }""",
                        ", ".join(tags),
                    )
                    logger.info("cda: uzupelniono ukryte pole tagow z formularza HAR")
                else:
                    logger.warning("cda: nie znaleziono pola tagow; pomijam tagi")

            report_manual_captions("cda", srt_path)
            terms = optional_visible_locator(page, ("#regulaminField",))
            if terms is not None:
                terms.set_checked(True)
            submit = unique_visible_locator(page, ("#dodajDoSerwisu",), "przycisku publikacji")
            logger.info("cda: formularz metadanych wypelniony; publikuje nagranie")
            submit.click()
            url = wait_for_video_url(
                page,
                r"cda\.pl/video/",
                platform="cda",
                cancel_check=self._raise_if_cancelled,
                heartbeat_probe=lambda: self._debug_snapshot(
                    page, "waiting_for_publication"
                ),
            )
            return UploadResult(
                success=True,
                platform_video_id=video_id_from_url(url),
                platform_url=url,
                captions_uploaded=False,
            )

    def add_to_playlist(
        self,
        platform_video_id: str,
        playlist_identifier: str,
        *,
        playlist_title: str | None = None,
    ) -> bool:
        logger.info("cda: automatyczne kolekcje/playlisty nie sa wspierane")
        return False
