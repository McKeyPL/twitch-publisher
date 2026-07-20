"""Uploader CDA oparty o aktualny, dwustopniowy formularz Playwright."""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from auth.browser_session import BrowserSessionManager
from config import BrowserConfig, BrowserPlatformConfig, RetryConfig
from uploaders.base import BaseUploader, UploadResult
from uploaders.browser_form import (
    HEARTBEAT_INTERVAL_MS,
    UPLOAD_TIMEOUT_MS,
    BrowserUploadError,
    capture_browser_debug,
    optional_visible_locator,
    report_manual_captions,
    should_retry_browser_error,
    unique_locator,
    unique_visible_locator,
    validate_upload_files,
    video_id_from_url,
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
CDA_STALE_REMOVE_SELECTOR = (
    "#uploader .videoContainer .fileListContainer "
    ".panel-heading-actions .icon-remove-sign"
)


def _clear_cda_stale_uploads(
    page: object,
    *,
    cancel_check: Callable[[], None] | None = None,
    timeout_seconds: float = 10.0,
) -> int:
    """Usuwa zakończone/błędne karty z listy uploadu przed wyborem pliku."""
    removed = 0
    while True:
        if cancel_check is not None:
            cancel_check()
        buttons = getattr(page, "locator")(CDA_STALE_REMOVE_SELECTOR)
        before = buttons.count()
        if before == 0:
            if removed:
                logger.info("cda: usunieto %d starych pozycji z listy uploadu", removed)
            return removed
        button = buttons.first
        title = button.get_attribute("title") or "Usuń z listy"
        logger.info(
            "cda: usuwam stara pozycje z listy uploadu (%s), pozostalo=%d",
            title,
            before,
        )
        # Stara karta bywa zwinięta i ikona nie ma wtedy pola w viewport. Handler
        # krzyżyka nadal istnieje w DOM; selektor jest ściśle ograniczony do listy
        # uploadów, dlatego wywołujemy natywne click() bez scrollowania strony.
        button.evaluate("element => element.click()")
        deadline = time.monotonic() + timeout_seconds
        while getattr(page, "locator")(CDA_STALE_REMOVE_SELECTOR).count() >= before:
            if cancel_check is not None:
                cancel_check()
            if time.monotonic() >= deadline:
                raise BrowserUploadError(
                    "CDA: kliknieto krzyzyk starego uploadu, ale pozycja nie "
                    "zniknela z listy",
                    retriable=True,
                )
            time.sleep(0.2)
        removed += 1


def _find_cda_submit_button(page: object) -> object:
    """Znajduje przycisk kończący publikację w obu znanych wariantach CDA."""
    return unique_visible_locator(
        page,
        (
            "button[type='button'][data-loading-text*='Dodaj do serwisu']",
            "button:has-text('Dodaj do serwisu')",
            "#dodajDoSerwisu",
            "button:has-text('Opublikuj w serwisie')",
            "input[type='submit'][value*='Dodaj do serwisu']",
            "input[type='submit'][value*='Opublikuj']",
        ),
        "przycisku Dodaj do serwisu",
    )


def _read_cda_upload_status(page: object) -> dict[str, Any]:
    """Czyta marker zakończenia oraz tekst panelu postępu bez zależności od ID."""
    result = getattr(page, "evaluate")(
        r"""() => {
            const visible = element => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && rect.width > 0 && rect.height > 0;
            };
            const normalize = value => (value || '').normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase();
            const bodyText = document.body ? document.body.innerText || '' : '';
            const normalizedBody = normalize(bodyText);
            const buttons = Array.from(document.querySelectorAll('button, input[type=submit]'))
                .filter(visible);
            const submit = buttons.find(element => {
                const text = normalize(element.innerText || element.value || '');
                const loading = normalize(element.getAttribute('data-loading-text') || '');
                return text.includes('dodaj do serwisu')
                    || text.includes('opublikuj w serwisie')
                    || loading.includes('dodaj do serwisu');
            });
            const progressNodes = Array.from(document.querySelectorAll(
                '.progress, .progress-bar, [role=progressbar], .qq-upload-list, '
                + '.qq-upload-status-text, .qq-upload-size, [class*=speed], [id*=speed]'
            )).filter(visible);
            let percent = null;
            for (const element of progressNodes) {
                const candidates = [
                    element.getAttribute('aria-valuenow'),
                    element.getAttribute('data-percent'),
                    element.getAttribute('data-progress'),
                    element.style && element.style.width,
                    element.innerText,
                ];
                for (const value of candidates) {
                    const match = String(value || '').match(/(\d+(?:[.,]\d+)?)\s*%?/);
                    if (match) {
                        const parsed = Number(match[1].replace(',', '.'));
                        if (Number.isFinite(parsed) && parsed >= 0 && parsed <= 100) {
                            percent = percent === null ? parsed : Math.max(percent, parsed);
                        }
                    }
                }
            }
            const details = Array.from(new Set(bodyText.split(/\r?\n/)
                .map(line => line.trim())
                .filter(line => line && (
                    /\d+(?:[.,]\d+)?\s*%/.test(line)
                    || /\d+(?:[.,]\d+)?\s*(?:k|m|g|t)?b\s*\/\s*s/i.test(line)
                    || /pr[eę]dko|pozosta|wysy[lł]|przes[lł]an|upload/i.test(line)
                )))).slice(0, 12);
            const completeMarker = normalizedBody.includes(
                'film zostal przeslany i oczekuje na publikacje'
            );
            const duplicateMatch = bodyText.match(
                /przes[lł]any film jest duplikatem[\s\S]{0,600}?(https?:\/\/(?:www\.)?cda\.pl\/video\/[A-Za-z0-9_-]+)/i
            );
            const successIcon = document.querySelector(
                '#uploader .fileListContainer .icon-file.icon-success'
            );
            const successContainer = successIcon
                ? (successIcon.closest('.panel') || successIcon.closest('.col-md-19')
                    || successIcon.parentElement)
                : null;
            const successLink = successContainer
                ? successContainer.querySelector('a[href*="/video/"]')
                : null;
            return {
                complete: completeMarker || Boolean(submit && !submit.disabled)
                    || Boolean(duplicateMatch) || Boolean(successLink),
                complete_marker: completeMarker,
                submit_ready: Boolean(submit && !submit.disabled),
                duplicate_url: duplicateMatch ? duplicateMatch[1] : null,
                success_url: successLink ? successLink.href : null,
                percent,
                details,
            };
        }"""
    )
    return dict(result or {})


def _wait_for_cda_upload_complete(
    page: object,
    *,
    cancel_check: Callable[[], None] | None = None,
    heartbeat_probe: Callable[[], None] | None = None,
    timeout_ms: int = UPLOAD_TIMEOUT_MS,
    heartbeat_interval_ms: int = HEARTBEAT_INTERVAL_MS,
) -> dict[str, Any]:
    """Czeka na gotowy przycisk publikacji i raportuje postęp/szybkość CDA."""
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    next_heartbeat = started
    while True:
        if cancel_check is not None:
            cancel_check()
        status = _read_cda_upload_status(page)
        if status.get("success_url"):
            logger.info(
                "cda: wykryto icon-file icon-success; URL filmu: %s",
                status["success_url"],
            )
            return status
        if status.get("duplicate_url"):
            logger.warning(
                "cda: serwis rozpoznal duplikat; wykorzystuje istniejacy URL: %s",
                status["duplicate_url"],
            )
            return status
        if status.get("complete"):
            logger.info(
                "cda: przesylanie zakonczone; formularz gotowy "
                "(postep=%s, marker=%s, przycisk=%s)",
                status.get("percent"),
                status.get("complete_marker"),
                status.get("submit_ready"),
            )
            return status
        now = time.monotonic()
        if now >= deadline:
            raise BrowserUploadError(
                "CDA: przekroczono limit oczekiwania na zakonczenie przesylania",
                retriable=True,
            )
        if now >= next_heartbeat:
            details = status.get("details") or []
            logger.info(
                "cda: przesylanie trwa (%.0f s), postep=%s; panel=%s",
                now - started,
                status.get("percent"),
                " | ".join(str(item) for item in details) or "brak czytelnych danych",
            )
            if heartbeat_probe is not None:
                heartbeat_probe()
            next_heartbeat = now + heartbeat_interval_ms / 1000
        time.sleep(min(1.0, max(0.05, deadline - now)))


def _cda_result_url(page: object) -> str | None:
    """Odczytuje link filmu z URL strony albo wyniku wyrenderowanego w DOM."""
    candidates: list[str] = [str(getattr(page, "url", ""))]
    # Nie skanujemy całej strony: nagłówek CDA zawiera linki /video/ z
    # powiadomień, które nie są wynikiem bieżącego uploadu.
    locator = getattr(page, "locator")(
        "#uploader .fileListContainer "
        ".panel:has(.icon-file.icon-success) a[href*='/video/']"
    )
    for index in range(min(locator.count(), 30)):
        item = locator.nth(index)
        href = item.get_attribute("href")
        if href:
            candidates.append(href)
    base_url = str(getattr(page, "url", ""))
    if not re.match(r"https?://", base_url):
        base_url = "https://www.cda.pl/"
    for candidate in candidates:
        absolute = urljoin(base_url, candidate.strip())
        match = re.search(r"https?://(?:www\.)?cda\.pl/video/[A-Za-z0-9_-]+", absolute)
        if match:
            return match.group(0)
    return None


def _wait_for_cda_result_url(
    page: object,
    *,
    cancel_check: Callable[[], None] | None = None,
    heartbeat_probe: Callable[[], None] | None = None,
    timeout_ms: int = UPLOAD_TIMEOUT_MS,
) -> str:
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    next_heartbeat = started + HEARTBEAT_INTERVAL_MS / 1000
    while True:
        if cancel_check is not None:
            cancel_check()
        url = _cda_result_url(page)
        if url:
            logger.info("cda: publikacja potwierdzona, URL filmu: %s", url)
            return url
        now = time.monotonic()
        if now >= deadline:
            raise BrowserUploadError(
                "CDA: kliknieto Dodaj do serwisu, ale nie uzyskano linku filmu. "
                "Sprawdz panel recznie; upload nie bedzie ponawiany.",
                retriable=False,
                manual_review_required=True,
            )
        if now >= next_heartbeat:
            logger.info(
                "cda: oczekuje na potwierdzenie i link filmu (%.0f s)",
                now - started,
            )
            if heartbeat_probe is not None:
                heartbeat_probe()
            next_heartbeat = now + HEARTBEAT_INTERVAL_MS / 1000
        time.sleep(min(1.0, max(0.05, deadline - now)))


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
            _clear_cda_stale_uploads(
                page,
                cancel_check=self._raise_if_cancelled,
            )
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

            upload_status = _wait_for_cda_upload_complete(
                page,
                cancel_check=self._raise_if_cancelled,
                heartbeat_probe=lambda: self._debug_snapshot(
                    page, "upload_progress"
                ),
            )
            duplicate_url = upload_status.get("duplicate_url")
            success_url = upload_status.get("success_url")
            if success_url:
                _clear_cda_stale_uploads(
                    page,
                    cancel_check=self._raise_if_cancelled,
                )
                logger.info("cda: upload zakonczony, URL filmu: %s", success_url)
                return UploadResult(
                    success=True,
                    platform_video_id=video_id_from_url(str(success_url)),
                    platform_url=str(success_url),
                    captions_uploaded=False,
                )
            if duplicate_url:
                _clear_cda_stale_uploads(
                    page,
                    cancel_check=self._raise_if_cancelled,
                )
                logger.info(
                    "cda: istniejacy film potwierdzony jako sukces: %s",
                    duplicate_url,
                )
                return UploadResult(
                    success=True,
                    platform_video_id=video_id_from_url(str(duplicate_url)),
                    platform_url=str(duplicate_url),
                    captions_uploaded=False,
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
            submit = _find_cda_submit_button(page)
            logger.info(
                "cda: formularz metadanych wypelniony; klikam Dodaj do serwisu"
            )
            submit.click()
            url = _wait_for_cda_result_url(
                page,
                cancel_check=self._raise_if_cancelled,
                heartbeat_probe=lambda: self._debug_snapshot(
                    page, "waiting_for_publication"
                ),
            )
            _clear_cda_stale_uploads(
                page,
                cancel_check=self._raise_if_cancelled,
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
