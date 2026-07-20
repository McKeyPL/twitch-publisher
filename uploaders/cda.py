"""Uploader CDA oparty o aktualny, dwustopniowy formularz Playwright."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from auth.browser_session import BrowserSessionManager
from config import BrowserConfig, BrowserPlatformConfig, RetryConfig
from uploaders.base import BaseUploader, UploadResult
from uploaders.browser_form import (
    BrowserUploadError,
    optional_visible_locator,
    report_manual_captions,
    unique_visible_locator,
    validate_upload_files,
    video_id_from_url,
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
        session_factory: Callable[[BrowserConfig], BrowserSessionManager] = BrowserSessionManager,
    ) -> None:
        super().__init__(retry_config)
        self.config = config
        self.browser_config = browser_config
        self._session_manager = session_factory(browser_config)

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
                should_retry=lambda exc: getattr(exc, "retriable", True),
            )
        except Exception as exc:
            logger.error("cda: upload %s nie powiodl sie: %s", video_path, exc)
            return UploadResult(success=False, error_message=str(exc))

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
            size_gib = video_path.stat().st_size / (1024 ** 3)
            logger.info(
                "cda: sesja gotowa (%s); wskazuje plik %.2f GiB: %s",
                page.url,
                size_gib,
                video_path,
            )
            file_input = unique_visible_locator(page, ("#js-upload-files",), "pliku wideo")
            file_input.set_input_files(str(video_path), timeout=60_000)
            logger.info(
                "cda: plik przekazany formularzowi; oczekuje na zakonczenie "
                "wysylania i formularz metadanych"
            )

            title_input = page.locator("#nazwa_wyswietlana")
            wait_for_visible_with_heartbeat(
                title_input,
                platform="cda",
                field_name="formularz metadanych",
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

            report_manual_captions("cda", srt_path)
            terms = optional_visible_locator(page, ("#regulaminField",))
            if terms is not None:
                terms.set_checked(True)
            submit = unique_visible_locator(page, ("#dodajDoSerwisu",), "przycisku publikacji")
            submit.click()
            url = wait_for_video_url(page, r"cda\.pl/video/")
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
