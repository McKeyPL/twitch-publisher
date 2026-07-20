"""Uploader Rumble oparty o formularz upload.php."""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from auth.browser_session import BrowserSessionManager
from config import BrowserConfig, BrowserPlatformConfig, RetryConfig
from uploaders.base import BaseUploader, UploadResult
from uploaders.browser_form import (
    BrowserUploadError,
    CANCEL_POLL_INTERVAL_MS,
    HEARTBEAT_INTERVAL_MS,
    UPLOAD_TIMEOUT_MS,
    capture_browser_debug,
    optional_visible_locator,
    report_manual_captions,
    should_retry_browser_error,
    unique_locator,
    unique_visible_locator,
    validate_upload_files,
    video_id_from_url,
    visible_error_text,
    wait_for_visible_with_heartbeat,
)


logger = logging.getLogger(__name__)
LICENSE_LABELS = {
    "0": "Personal Use",
    "5": "Video Management (exclusive)",
    "6": "Rumble Only (non-exclusive)",
    "7": "Video Management (excluding YouTube)",
}


def _set_category_by_label(page: object, input_selector: str, label: str) -> bool:
    options = getattr(page, "locator")(".select-option[data-value][data-label]")
    value = options.evaluate_all(
        r"""(elements, wanted) => {
            const normalize = value => value
                .trim()
                .toLocaleLowerCase()
                .replace(/\s*\(video game\)\s*$/, '');
            const normalized = normalize(wanted);
            const match = elements.find(
                element => normalize(element.dataset.label || '') === normalized
            );
            return match ? match.dataset.value : null;
        }""",
        label,
    )
    if not value:
        return False
    hidden = getattr(page, "locator")(input_selector)
    if hidden.count() != 1:
        return False
    hidden.evaluate(
        """(element, selectedValue) => {
            element.value = selectedValue;
            element.dispatchEvent(new Event('input', {bubbles: true}));
            element.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        value,
    )
    return True


def _rumble_result_url(page: object) -> str:
    direct = getattr(page, "locator")("#form3 textarea#direct")
    if direct.count() == 1:
        value = (direct.input_value() or "").strip()
        if value:
            return urljoin("https://rumble.com", value)
    link = getattr(page, "locator")("#form3 p#view a[href]")
    if link.count() == 1:
        value = (link.get_attribute("href") or "").strip()
        if value:
            return urljoin("https://rumble.com", value)
    raise BrowserUploadError(
        "Rumble pokazal sukces, ale nie zwrocil URL nagrania; sprawdz panel recznie",
        retriable=False,
        manual_review_required=True,
    )


def _accept_rumble_confirmation(page: object, checkbox_id: str) -> None:
    """Zaznacza ukryty checkbox i sprawdza pole walidowane przez Rumble.

    Natywne inputy ``#crights`` i ``#cterms`` sa ukryte przez CSS. Klikniecie
    tekstu wewnatrz etykiety ``cterms`` moze trafic w link do regulaminu zamiast
    checkboxa. Wymuszone ``set_checked`` emituje zdarzenie ``change`` obslugiwane
    przez strone i aktualizuje ``#rights``/``#terms``.
    """
    checkbox = unique_locator(
        page,
        (f"#{checkbox_id}",),
        f"potwierdzenia {checkbox_id}",
    )
    if checkbox.is_checked():
        pass
    else:
        checkbox.evaluate(
            """element => {
                element.checked = true;
                element.dispatchEvent(new Event('input', {bubbles: true}));
                element.dispatchEvent(new Event('change', {bubbles: true}));
            }"""
        )
    if not checkbox.is_checked():
        raise BrowserUploadError(
            f"Rumble: kontrolka zgody {checkbox_id} nie zostala zaznaczona",
            retriable=False,
        )
    hidden_id = checkbox_id.removeprefix("c")
    hidden = unique_locator(
        page,
        (f"#{hidden_id}",),
        f"ukrytego potwierdzenia {hidden_id}",
    )
    if hidden.input_value() != "1":
        raise BrowserUploadError(
            f"Rumble: zgoda {checkbox_id} nie zaktualizowala pola {hidden_id}",
            retriable=False,
        )


def _read_rumble_transfer_status(page: object) -> dict[str, object]:
    """Czyta serwerowy token uploadu i widoczny postep z formularza Rumble."""
    status = getattr(page, "evaluate")(
        """() => {
            const videoToken = (document.getElementById('video[]')?.value || '').trim();
            const progressTexts = Array.from(
                document.querySelectorAll('.top_percent, .num_percent')
            ).map(element => (element.textContent || '').trim()).filter(Boolean);
            const percentages = progressTexts.flatMap(text =>
                Array.from(text.matchAll(/(\\d+(?:[.,]\\d+)?)\\s*%/g), match =>
                    Number.parseFloat(match[1].replace(',', '.'))
                )
            ).filter(Number.isFinite);
            const error = Array.from(document.querySelectorAll(
                '#error_video, #error_files, #error_files_2, .upload-error'
            )).find(element => {
                const style = window.getComputedStyle(element);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                    (element.textContent || '').trim();
            });
            return {
                complete: Boolean(videoToken) && videoToken !== 'ERROR',
                failed: videoToken === 'ERROR',
                percent: percentages.length ? Math.max(...percentages) : null,
                details: [...new Set(progressTexts)],
                error: error ? (error.textContent || '').trim() : null
            };
        }"""
    )
    if not isinstance(status, dict):
        raise BrowserUploadError(
            "Rumble: nie udalo sie odczytac stanu transferu",
            retriable=True,
        )
    return status


def _wait_for_rumble_transfer(
    page: object,
    *,
    cancel_check: Callable[[], None] | None = None,
    heartbeat_probe: Callable[[], None] | None = None,
    timeout_ms: int = UPLOAD_TIMEOUT_MS,
    heartbeat_interval_ms: int = HEARTBEAT_INTERVAL_MS,
) -> None:
    """Czeka na token ustawiany przez Rumble po wyslaniu i scaleniu pliku."""
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    next_heartbeat = started
    while True:
        if cancel_check is not None:
            cancel_check()
        status = _read_rumble_transfer_status(page)
        if status.get("failed") or status.get("error"):
            detail = status.get("error") or "Rumble oznaczyl transfer jako ERROR"
            raise BrowserUploadError(
                f"Rumble: transfer pliku nie powiodl sie: {detail}",
                retriable=True,
            )
        if status.get("complete"):
            logger.info(
                "rumble: transfer pliku i scalenie chunkow zakonczone (100%%)"
            )
            return

        now = time.monotonic()
        if now >= deadline:
            raise BrowserUploadError(
                f"Rumble: przekroczono limit oczekiwania na transfer "
                f"({timeout_ms / 1000:.0f} s)",
                retriable=True,
            )
        if now >= next_heartbeat:
            details = "; ".join(str(item) for item in status.get("details", []))
            logger.info(
                "rumble: transfer trwa (%.0f s), postep=%s, panel=%s",
                now - started,
                status.get("percent") if status.get("percent") is not None else "?",
                details or "brak danych",
            )
            if heartbeat_probe is not None:
                heartbeat_probe()
            next_heartbeat = now + heartbeat_interval_ms / 1000
        remaining_ms = max(1, int((deadline - now) * 1000))
        getattr(page, "wait_for_timeout")(
            min(CANCEL_POLL_INTERVAL_MS, remaining_ms)
        )


class RumbleUploader(BaseUploader):
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
            platform="rumble",
            debug_directory=self.browser_config.debug_directory,
            stage=stage,
            take_screenshot=take_screenshot,
        )
        if take_screenshot:
            self._last_debug_screenshot = now

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
            license_option = self.config.license_option
            if license_option not in LICENSE_LABELS:
                allowed = ", ".join(
                    f"{code}={label}" for code, label in LICENSE_LABELS.items()
                )
                raise BrowserUploadError(
                    "Ustaw RUMBLE_LICENSE_OPTION w .env przed uploadem Rumble. "
                    f"Dozwolone wartosci: {allowed}",
                    retriable=False,
                )
            if self.config.max_file_size_gb is not None:
                limit_bytes = self.config.max_file_size_gb * 1_000_000_000
                if video_path.stat().st_size > limit_bytes:
                    raise BrowserUploadError(
                        f"Plik przekracza limit Rumble {self.config.max_file_size_gb:g} GB: "
                        f"{video_path}",
                        retriable=False,
                    )
            return self._with_retry(
                lambda: self._upload_once(video_path, title, description, tags, srt_path),
                operation_name=f"upload {video_path.name}",
                should_retry=should_retry_browser_error,
            )
        except Exception as exc:
            logger.error("rumble: upload %s nie powiodl sie: %s", video_path, exc)
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
        with self._session_manager.open(
            "rumble", self.config, self._is_authenticated
        ) as session:
            page = session.page
            self._raise_if_cancelled()
            self._debug_snapshot(page, "session_ready", force=True)
            logger.info(
                "rumble: sesja gotowa (%s); wskazuje plik %.2f GiB: %s",
                page.url,
                video_path.stat().st_size / (1024 ** 3),
                video_path,
            )
            unique_visible_locator(page, ("#Filedata",), "pliku wideo").set_input_files(
                str(video_path), timeout=60_000
            )
            self._raise_if_cancelled()
            self._debug_snapshot(page, "file_selected", force=True)
            unique_visible_locator(page, ("#title",), "tytulu").fill(title)
            unique_visible_locator(page, ("#description",), "opisu").fill(description)
            tags_input = optional_visible_locator(page, ("#tags",))
            if tags_input is not None:
                tags_input.fill(", ".join(tags))
            visibility = optional_visible_locator(page, ("#visibility_unlisted",))
            if visibility is not None:
                visibility.set_checked(True)

            primary_category = self.config.primary_category or "Gaming"
            if not _set_category_by_label(
                page, "#category_primary", primary_category
            ):
                raise BrowserUploadError(
                    f"Rumble: nie znaleziono kategorii glownej {primary_category!r}",
                    retriable=False,
                )
            logger.info("rumble: ustawiono kategorie glowna %s", primary_category)
            if len(tags) >= 4:
                game = tags[-1]
                if _set_category_by_label(page, "#category_secondary", game):
                    logger.info("rumble: ustawiono kategorie gry %s", game)
                else:
                    logger.info(
                        "rumble: brak dokladnej kategorii gry %s; pozostaje Gaming",
                        game,
                    )

            report_manual_captions("rumble", srt_path)
            unique_visible_locator(page, ("#submitForm",), "przycisku Upload").click()

            # Rumble pokazuje drugi krok dotyczacy praw/licencji. Pola opisowe sa
            # opcjonalne, lecz potwierdzenie praw i regulaminu jest wymagane.
            final_submit = page.locator("#submitForm2")
            wait_for_visible_with_heartbeat(
                final_submit,
                platform="rumble",
                field_name="drugi krok formularza",
                failure_probe=lambda: visible_error_text(
                    page,
                    (
                        "#error_files",
                        "#error_files_2",
                        "#error_video",
                        "#error_title",
                        "#error_description",
                        "#error_categories",
                        ".upload-error",
                    ),
                ),
                cancel_check=self._raise_if_cancelled,
                heartbeat_probe=lambda: self._debug_snapshot(
                    page, "waiting_for_second_step"
                ),
            )
            self._debug_snapshot(page, "second_step_ready", force=True)
            _wait_for_rumble_transfer(
                page,
                cancel_check=self._raise_if_cancelled,
                heartbeat_probe=lambda: self._debug_snapshot(
                    page, "waiting_for_transfer"
                ),
            )
            self._debug_snapshot(page, "transfer_complete", force=True)
            license_option = self.config.license_option or ""
            license_control = unique_visible_locator(
                page,
                (f"[crcval='{license_option}']",),
                "opcji licencji Rumble",
            )
            license_control.click()
            logger.info(
                "rumble: ustawiono licencje %s=%s",
                license_option,
                LICENSE_LABELS[license_option],
            )
            for checkbox_id in ("crights", "cterms"):
                _accept_rumble_confirmation(page, checkbox_id)
            logger.info("rumble: zaakceptowano prawa do materialu i regulamin")
            self._debug_snapshot(
                page, "license_and_confirmations_ready", force=True
            )
            final_submit.click()
            self._debug_snapshot(page, "final_submit_clicked", force=True)
            success_form = page.locator("#form3")
            try:
                wait_for_visible_with_heartbeat(
                    success_form,
                    platform="rumble",
                    field_name="potwierdzenie publikacji",
                    failure_probe=lambda: visible_error_text(
                        page,
                        (
                            "#error_featured",
                            "#error_rights",
                            "#error_terms",
                            "#error_licenseExtra",
                            "#error_unknown",
                            "#error_unknown_2",
                        ),
                    ),
                    cancel_check=self._raise_if_cancelled,
                    heartbeat_probe=lambda: self._debug_snapshot(
                        page, "waiting_for_publication"
                    ),
                )
            except BrowserUploadError as exc:
                raise BrowserUploadError(
                    f"Nie potwierdzono wyniku publikacji Rumble: {exc}. "
                    "Sprawdz panel recznie; upload nie bedzie ponawiany.",
                    retriable=False,
                    manual_review_required=True,
                ) from exc
            url = _rumble_result_url(page)
            if not re.search(r"rumble\.com/(v|embed|account/content)", url):
                raise BrowserUploadError(
                    f"Rumble zwrocil nieoczekiwany URL: {url}; sprawdz panel recznie",
                    retriable=False,
                    manual_review_required=True,
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
        logger.info("rumble: automatyczne playlisty/kolekcje nie sa wspierane")
        return False
