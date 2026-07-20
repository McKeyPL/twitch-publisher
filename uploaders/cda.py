"""Uploader CDA oparty o aktualny, dwustopniowy formularz Playwright."""

from __future__ import annotations

import logging
import re
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
CDA_FORM_DEFAULTS = {
    "private": False,
    "accept_terms": True,
    "confirm_rights": True,
    "contains_violence": False,
    "contains_sex": False,
    "contains_addiction": False,
    "contains_profanity": False,
    "contains_sponsorship": False,
}


def _set_checkbox_by_text(page: object, text_fragment: str, checked: bool) -> bool:
    return bool(
        getattr(page, "evaluate")(
            r"""({fragment, checked}) => {
                const normalize = value => (value || '').normalize('NFD')
                    .replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase().trim();
                const wanted = normalize(fragment);
                const labels = Array.from(document.querySelectorAll('label'));
                let label = labels.find(item => normalize(item.innerText).includes(wanted));
                let input = label && (label.control || label.querySelector('input[type=checkbox]'));
                if (!input && label && label.htmlFor) input = document.getElementById(label.htmlFor);
                if (!input) {
                    const containers = Array.from(document.querySelectorAll('body *'))
                        .filter(item => normalize(item.innerText).includes(wanted)
                            && item.querySelectorAll('input[type=checkbox]').length === 1)
                        .sort((a, b) => a.innerText.length - b.innerText.length);
                    input = containers[0] && containers[0].querySelector('input[type=checkbox]');
                }
                if (!input) return false;
                if (Boolean(input.checked) !== Boolean(checked)) input.click();
                return Boolean(input.checked) === Boolean(checked);
            }""",
            {"fragment": text_fragment, "checked": checked},
        )
    )


def _set_radio_by_question(page: object, question_fragment: str, answer_yes: bool) -> bool:
    return bool(
        getattr(page, "evaluate")(
            r"""({question, answerYes}) => {
                const normalize = value => (value || '').normalize('NFD')
                    .replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase().trim();
                const wanted = normalize(question);
                const containers = Array.from(document.querySelectorAll('body *'))
                    .filter(item => normalize(item.innerText).includes(wanted)
                        && item.querySelectorAll('input[type=radio]').length >= 2)
                    .sort((a, b) => a.innerText.length - b.innerText.length);
                const container = containers[0];
                if (!container) return false;
                const radios = Array.from(container.querySelectorAll('input[type=radio]'));
                const answer = answerYes ? 'tak' : 'nie';
                let target = radios.find(input => {
                    const label = input.labels && input.labels[0];
                    return label && normalize(label.innerText) === answer;
                });
                if (!target) target = radios[answerYes ? 0 : 1];
                if (!target) return false;
                if (!target.checked) target.click();
                return Boolean(target.checked);
            }""",
            {"question": question_fragment, "answerYes": answer_yes},
        )
    )


def _find_cda_title_input(page: object, video_path: Path) -> object:
    locator_factory = getattr(page, "locator")
    for selector in ("#nazwa_wyswietlana", "input[name='nazwa_wyswietlana']"):
        locator = locator_factory(selector)
        if locator.count() == 1 and locator.is_visible():
            return locator

    candidates = locator_factory("input[type='text']:visible")
    video_words = {
        word for word in re.split(r"[^a-z0-9]+", video_path.stem.casefold()) if len(word) >= 3
    }
    ranked: list[tuple[int, object]] = []
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        placeholder = (candidate.get_attribute("placeholder") or "").casefold()
        identity = " ".join(
            filter(
                None,
                (
                    candidate.get_attribute("id"),
                    candidate.get_attribute("name"),
                ),
            )
        ).casefold()
        if any(word in placeholder or word in identity for word in ("tag", "szuk", "search")):
            continue
        value = (candidate.input_value() or "").casefold()
        value_words = set(re.split(r"[^a-z0-9]+", value))
        score = len(video_words & value_words) + (10 if value.strip() else 0)
        ranked.append((score, candidate))
    if ranked:
        score, candidate = max(ranked, key=lambda item: item[0])
        if score > 0:
            return candidate
    raise BrowserUploadError(
        "CDA: formularz jest widoczny, ale nie znaleziono pola tytulu",
        retriable=False,
    )


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

            ready_marker = page.get_by_text(
                re.compile(
                    r"Film zosta[łl] przes[łl]any.*oczekuje na publikacj[eę]",
                    re.IGNORECASE,
                )
            ).first
            wait_for_visible_with_heartbeat(
                ready_marker,
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
            title_input = _find_cda_title_input(page, video_path)
            title_input.fill(title)
            unique_visible_locator(
                page,
                (
                    "textarea[name='opis']",
                    "textarea[placeholder*='Opis']",
                    "textarea[placeholder*='opis']",
                ),
                "opisu",
            ).fill(description)
            tags_input = optional_visible_locator(
                page,
                (
                    "#tags_tag",  # pole tworzone przez widget jQuery tagsInput
                    "input[name='tagi']:not([type='hidden'])",
                    "#tags:not([type='hidden'])",
                    "input[placeholder*='Tagi']",
                    "input[placeholder*='tagi']",
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

            options = {**CDA_FORM_DEFAULTS, **self.config.form_options}
            checkbox_answers = (
                ("Plik niepubliczny", options["private"]),
                ("Akceptuje regulamin", options["accept_terms"]),
                ("jestem uprawniony", options["confirm_rights"]),
            )
            for label, checked in checkbox_answers:
                if not _set_checkbox_by_text(page, label, checked):
                    raise BrowserUploadError(
                        f"CDA: nie znaleziono lub nie ustawiono pola {label!r}",
                        retriable=False,
                    )

            radio_answers = (
                ("zawiera przemoc", options["contains_violence"]),
                ("sceny seksu", options["contains_sex"]),
                ("sceny z uzaleznieniami", options["contains_addiction"]),
                ("zawiera wulgaryzmy", options["contains_profanity"]),
                ("lokowanie produktu", options["contains_sponsorship"]),
            )
            for question, answer_yes in radio_answers:
                if not _set_radio_by_question(page, question, answer_yes):
                    raise BrowserUploadError(
                        f"CDA: nie znaleziono odpowiedzi dla pytania {question!r}",
                        retriable=False,
                    )

            report_manual_captions("cda", srt_path)
            submit = unique_visible_locator(
                page,
                (
                    "#dodajDoSerwisu",
                    "button:has-text('Opublikuj w serwisie')",
                    "input[type='submit'][value*='Opublikuj']",
                ),
                "przycisku publikacji",
            )
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
