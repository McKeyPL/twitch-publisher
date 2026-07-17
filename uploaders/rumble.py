"""Uploader Rumble oparty o formularz upload.php."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from auth.browser_session import BrowserSessionManager
from config import BrowserConfig, BrowserPlatformConfig, RetryConfig
from uploaders.base import BaseUploader, UploadResult
from uploaders.browser_form import (
    optional_visible_locator,
    report_manual_captions,
    unique_visible_locator,
    validate_upload_files,
    video_id_from_url,
    wait_for_video_url,
)


logger = logging.getLogger(__name__)


class RumbleUploader(BaseUploader):
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
        return "rumble"

    @staticmethod
    def _is_authenticated(page: object) -> bool:
        url = str(getattr(page, "url", "")).lower()
        if "auth.rumble.com" in url or "login" in url or "sign-in" in url:
            return False
        return getattr(page, "locator")("#Filedata").count() == 1

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
            logger.error("rumble: upload %s nie powiodl sie: %s", video_path, exc)
            return UploadResult(success=False, error_message=str(exc))

    def _upload_once(
        self,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str],
        srt_path: Path | None,
    ) -> UploadResult:
        with self._session_manager.open(
            "rumble", self.config, self._is_authenticated
        ) as session:
            page = session.page
            unique_visible_locator(page, ("#Filedata",), "pliku wideo").set_input_files(
                str(video_path)
            )
            unique_visible_locator(page, ("#title",), "tytulu").fill(title)
            unique_visible_locator(page, ("#description",), "opisu").fill(description)
            tags_input = optional_visible_locator(page, ("#tags",))
            if tags_input is not None:
                tags_input.fill(", ".join(tags))
            visibility = optional_visible_locator(page, ("#visibility_unlisted",))
            if visibility is not None:
                visibility.set_checked(True)

            report_manual_captions("rumble", srt_path)
            unique_visible_locator(page, ("#submitForm",), "przycisku Upload").click()

            # Rumble pokazuje drugi krok dotyczacy praw/licencji. Pola opisowe sa
            # opcjonalne, lecz potwierdzenie praw i regulaminu jest wymagane.
            final_submit = page.locator("#submitForm2")
            final_submit.wait_for(state="visible", timeout=12 * 60 * 60 * 1000)
            for selector in ("#crights", "#cterms"):
                checkbox = optional_visible_locator(page, (selector,))
                if checkbox is not None:
                    checkbox.set_checked(True)
            final_submit.click()

            url = wait_for_video_url(page, r"rumble\.com/(v|embed|account/content)")
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
        logger.info("rumble: automatyczne playlisty/kolekcje nie sa wspierane")
        return False
