"""Bounded YouTube Studio inspection and irreversible action execution."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .diagnostics import DiagnosticRun
from .models import RemediationAction
from .studio_parser import ParsedStudioClaim, StudioClaimParser


logger = logging.getLogger(__name__)

_ALIASES = {
    "see_details": ("see details", "review issues", "zobacz szczegóły", "sprawdź problemy"),
    "take_action": (
        "take action",
        "select action",
        "actions",
        "podejmij działanie",
        "wybierz działanie",
        "działania",
    ),
    "erase_song": ("erase song", "remove song", "usuń utwór", "wymaż utwór"),
    "mute_all": (
        "mute all sound in the claimed segment",
        "mute all sound in claimed segments",
        "mute all audio",
        "wycisz cały dźwięk",
    ),
    "trim": ("trim out segment", "trim segment", "wytnij fragment", "przytnij fragment"),
    "continue": ("continue", "dalej", "kontynuuj"),
    "save": ("save", "zapisz"),
    "confirm_mute": ("mute", "wycisz"),
    "confirm_trim": ("trim", "przytnij", "wytnij"),
}

_PROCESSING_MARKERS = (
    "video editing is in progress",
    "processing your edits",
    "trwa edytowanie filmu",
    "przetwarzanie zmian",
)
_SUBMITTED_MARKERS = _PROCESSING_MARKERS + (
    "edit submitted",
    "changes are being processed",
    "zmiany są przetwarzane",
)

_CLAIM_UI_READY_SCRIPT = r"""
() => {
  const rows = document.querySelector(
    'ytcr-video-content-list-row, ytcp-video-copyright-claim-row, ' +
    'ytcp-video-copyright-claim-details, ytcp-copyright-claim-row, ' +
    '[data-testid*="claim"], [class*="claim-row"]'
  );
  if (rows) return true;
  const text = (document.body && document.body.innerText || '').toLowerCase();
  return /video editing is in progress|processing your edits|trwa edytowanie filmu|przetwarzanie zmian/.test(text);
}
"""


class StudioAutomationUnavailable(RuntimeError):
    pass


class StudioAmbiguousUi(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StudioInspection:
    video_id: str
    processing: bool
    claims: tuple[ParsedStudioClaim, ...]
    page_url: str


@dataclass(frozen=True, slots=True)
class StudioExecutionResult:
    submitted: bool
    dry_run: bool
    action: RemediationAction
    trace_path: Path | None
    before_screenshot: Path | None = None
    confirmation_screenshot: Path | None = None
    after_screenshot: Path | None = None


class StudioCopyrightExecutor:
    def __init__(self, page: Any, diagnostic: DiagnosticRun) -> None:
        self.page = page
        self.diagnostic = diagnostic
        self.parser = StudioClaimParser()

    def inspect(self, video_id: str) -> StudioInspection:
        url = f"https://studio.youtube.com/video/{video_id}/copyright?hl=en"
        claim_data_error: Exception | None = None
        claim_data_status: int | None = None
        try:
            with self.page.expect_response(
                lambda response: "creator/list_creator_received_claims" in response.url,
                timeout=30_000,
            ) as response_info:
                self.page.goto(url, wait_until="domcontentloaded")
            claim_data_status = response_info.value.status
            if claim_data_status >= 400:
                claim_data_error = StudioAutomationUnavailable(
                    f"Studio claims request returned HTTP {claim_data_status}"
                )
        except Exception as exc:
            claim_data_error = exc
        self._validate_video_id(video_id)
        # The response completes shortly before Polymer renders the claim rows.
        self.page.wait_for_timeout(750)
        try:
            self.page.wait_for_function(_CLAIM_UI_READY_SCRIPT, timeout=10_000)
        except Exception:
            logger.debug(
                "Studio claim UI did not expose a terminal element within 10 seconds"
            )
        processing = self._body_contains(_PROCESSING_MARKERS)
        claims = self.parser.extract(self.page, video_id)
        if not claims and not processing:
            details = self._visible_locators("button", _ALIASES["see_details"])
            if len(details) == 1:
                details[0].click()
                self.page.wait_for_timeout(500)
                self._validate_video_id(video_id)
                claims = self.parser.extract(self.page, video_id)
        before = self.diagnostic.screenshot(self.page, "copyright_page")
        body_excerpt = self.page.locator("body").inner_text(timeout=10_000)[:4000]
        self.diagnostic.write_json(
            "inspection",
            {
                "video_id": video_id,
                "url": self.page.url.split("?", 1)[0],
                "processing": processing,
                "claims": [item.claim for item in claims],
                "claim_data_status": claim_data_status,
                "claim_data_error": str(claim_data_error) if claim_data_error else None,
                "body_excerpt": body_excerpt,
                "screenshot": str(before) if before else None,
            },
        )
        if claim_data_error is not None and not claims and not processing:
            raise StudioAutomationUnavailable(
                "Studio did not finish loading Content ID claim data: "
                f"{claim_data_error}"
            ) from claim_data_error
        return StudioInspection(video_id, processing, tuple(claims), self.page.url)

    def execute(
        self,
        video_id: str,
        parsed_claim: ParsedStudioClaim,
        action: RemediationAction,
        *,
        dry_run: bool,
        trace_path: Path | None,
    ) -> StudioExecutionResult:
        self._validate_video_id(video_id)
        before = self.diagnostic.screenshot(self.page, f"before_{action.value.lower()}")
        take_action_buttons = self._visible_locators("button", _ALIASES["take_action"])
        if parsed_claim.dom_index >= len(take_action_buttons):
            raise StudioAmbiguousUi(
                "The parsed claim cannot be matched to one Take action button"
            )
        take_action_buttons[parsed_claim.dom_index].click()
        self.page.wait_for_timeout(250)

        if action is RemediationAction.TRIM:
            self._click_unique_any_role(_ALIASES["trim"])
        else:
            self._click_unique_any_role(_ALIASES["erase_song"])
        self.page.wait_for_timeout(500)
        self._validate_video_id(video_id)

        if action is RemediationAction.MUTE_ALL:
            self._click_unique_any_role(_ALIASES["mute_all"])

        confirmation = self.diagnostic.screenshot(
            self.page, f"confirmation_{action.value.lower()}"
        )
        self.diagnostic.write_json(
            "planned_action",
            {
                "video_id": video_id,
                "claim": parsed_claim.claim,
                "action": action,
                "dry_run": dry_run,
            },
        )
        if dry_run:
            self.page.keyboard.press("Escape")
            return StudioExecutionResult(
                False,
                True,
                action,
                trace_path,
                before,
                confirmation,
                None,
            )

        self._submit_confirmation(action)
        self._wait_for_submitted_marker()
        after = self.diagnostic.screenshot(self.page, f"submitted_{action.value.lower()}")
        return StudioExecutionResult(
            True,
            False,
            action,
            trace_path,
            before,
            confirmation,
            after,
        )

    def _submit_confirmation(self, action: RemediationAction) -> None:
        # Studio occasionally presents Continue before the irreversible final
        # confirmation. Bound the sequence and never click an unrelated button.
        continue_buttons = self._visible_exact_locators("button", _ALIASES["continue"])
        if len(continue_buttons) > 1:
            raise StudioAmbiguousUi("More than one visible Continue button")
        if continue_buttons:
            continue_buttons[0].click()
            self.page.wait_for_timeout(300)

        final_aliases = (
            _ALIASES["confirm_trim"]
            if action is RemediationAction.TRIM
            else _ALIASES["confirm_mute"]
        )
        final_buttons = self._visible_exact_locators("button", final_aliases)
        if not final_buttons:
            final_buttons = self._visible_exact_locators("button", _ALIASES["save"])
        if len(final_buttons) != 1:
            raise StudioAmbiguousUi(
                f"Expected one final {action.value} confirmation, got {len(final_buttons)}"
            )
        final_buttons[0].click()

    def _wait_for_submitted_marker(self) -> None:
        pattern = _pattern(_SUBMITTED_MARKERS)
        marker = self.page.get_by_text(pattern, exact=False).first
        try:
            marker.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise StudioAutomationUnavailable(
                "Studio did not confirm that the edit was submitted"
            ) from exc

    def _validate_video_id(self, video_id: str) -> None:
        if f"/video/{video_id}/" not in self.page.url:
            raise StudioAmbiguousUi(
                f"Studio URL does not identify expected video {video_id}: {self.page.url}"
            )

    def _body_contains(self, values: Iterable[str]) -> bool:
        body = self.page.locator("body").inner_text(timeout=10_000).lower()
        return any(value in body for value in values)

    def _click_unique_any_role(self, aliases: Iterable[str]) -> None:
        for role in ("menuitem", "option", "radio", "button"):
            unique = _unique_locators(self._visible_locators(role, aliases))
            if len(unique) == 1:
                unique[0].click()
                return
            if len(unique) > 1:
                raise StudioAutomationUnavailable(
                    f"More than one {role} matches Studio action {tuple(aliases)}"
                )
        raise StudioAutomationUnavailable(
            f"No Studio action matches {tuple(aliases)}"
        )

    def _visible_exact_locators(self, role: str, aliases: Iterable[str]) -> list[Any]:
        locator = self.page.get_by_role(
            role,
            name=re.compile(
                "^(?:" + "|".join(re.escape(value) for value in aliases) + ")$",
                re.IGNORECASE,
            ),
            exact=True,
        )
        result: list[Any] = []
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                result.append(candidate)
        return result

    def _visible_locators(self, role: str, aliases: Iterable[str]) -> list[Any]:
        locator = self.page.get_by_role(role, name=_pattern(aliases), exact=False)
        result: list[Any] = []
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                result.append(candidate)
        return result


def _pattern(aliases: Iterable[str]) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(value) for value in aliases), re.IGNORECASE)


def _unique_locators(locators: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for locator in locators:
        try:
            key = str(locator.evaluate("element => element.outerHTML"))
        except Exception:
            key = repr(locator)
        if key not in seen:
            seen.add(key)
            result.append(locator)
    return result
