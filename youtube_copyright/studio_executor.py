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
    "see_details": ("view details", "see details", "review issues"),
    "take_action": ("take action", "select action", "actions"),
    "erase_song": ("erase song", "remove song"),
    "mute_all": (
        "mute all sound in the claimed segment",
        "mute all sound in the claimed segments",
        "mute all sound in claimed segments",
        "mute all audio",
    ),
    "trim": ("trim out segment", "trim segment"),
    "continue": ("continue",),
    "save": ("save",),
    "confirm_changes": ("confirm changes",),
    "confirm_mute": ("mute",),
    "confirm_trim": ("trim",),
}

_PROCESSING_MARKERS = (
    "video editing is in progress",
    "editing video",
    "still processing",
    "processing your edits",
)
_SUBMITTED_MARKERS = _PROCESSING_MARKERS + (
    "edit submitted",
    "changes are being processed",
    "changes saved",
)

_SUBMISSION_CONFIRMED_SCRIPT = r"""
markers => {
  const bodyText = (document.body && document.body.innerText || '').toLowerCase();
  if (markers.some(marker => bodyText.includes(marker))) return true;
  const visible = element => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      rect.width > 0 && rect.height > 0;
  };
  return Array.from(document.querySelectorAll('[role="progressbar"]')).some(element =>
    visible(element) &&
    /operation in progress/i.test(element.getAttribute('aria-label') || '')
  );
}
"""

_CLAIM_UI_READY_SCRIPT = r"""
() => {
  const rows = document.querySelector(
    'ytcr-video-content-list-row, ytcp-video-copyright-claim-row, ' +
    'ytcp-video-copyright-claim-details, ytcp-copyright-claim-row, ' +
    '[data-testid*="claim"], [class*="claim-row"]'
  );
  if (rows) return true;
  const text = (document.body && document.body.innerText || '').toLowerCase();
  return /video editing is in progress|editing video|still processing|processing your edits/.test(text);
}
"""

_VISIBLE_INDEXES_SCRIPT = r"""
elements => elements.flatMap((element, index) => {
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.visibility !== 'hidden' && style.display !== 'none' &&
    rect.width > 0 && rect.height > 0 ? [index] : [];
})
"""


class StudioAutomationUnavailable(RuntimeError):
    pass


class StudioSubmissionUncertain(StudioAutomationUnavailable):
    """The irreversible confirmation was clicked but its outcome is unknown."""


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
        # Studio automation intentionally targets one stable UI language. Account
        # language may vary, therefore every entry URL explicitly requests English.
        url = f"https://studio.youtube.com/video/{video_id}/claims?hl=en"
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
                "claim_rows": [
                    {
                        "dom_index": item.dom_index,
                        "raw_text": item.raw_text,
                        "claim": item.claim,
                    }
                    for item in claims
                ],
                "studio_language": "en",
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
        # The current Studio flow has three separate screens:
        # action picker -> edit settings (Save) -> permanent-edit acknowledgement.
        # Older variants can omit either intermediate screen, so every transition
        # is detected from the visible controls instead of assuming one layout.
        continue_buttons = self._visible_exact_locators("button", _ALIASES["continue"])
        if len(continue_buttons) > 1:
            raise StudioAmbiguousUi("More than one visible Continue button")
        if continue_buttons:
            continue_buttons[0].click()
            self.page.wait_for_timeout(300)

        # Some older Studio variants show the acknowledgement immediately.
        if self._accept_confirmation_checkbox():
            self._click_final_confirmation(action, allow_save=True)
            return

        # In the current UI this commits the edit settings and only then opens
        # the irreversible-change dialog containing the checkbox.
        self._click_final_confirmation(action, allow_save=True)
        self.page.wait_for_timeout(500)
        self.diagnostic.screenshot(self.page, "permanent_edit_confirmation")

        if not self._accept_confirmation_checkbox():
            # Trim and some legacy flows submit directly on the previous click.
            return

        confirm_buttons = self._visible_exact_locators(
            "button", _ALIASES["confirm_changes"]
        )
        if len(confirm_buttons) != 1:
            raise StudioAmbiguousUi(
                "Expected one permanent-edit confirmation button, "
                f"got {len(confirm_buttons)}"
            )
        confirm_button = confirm_buttons[0]
        if confirm_button.get_attribute("aria-disabled") == "true":
            raise StudioAutomationUnavailable(
                "Confirm changes remained disabled after accepting the acknowledgement"
            )
        confirm_button.click(timeout=10_000)

    def _click_final_confirmation(
        self,
        action: RemediationAction,
        *,
        allow_save: bool,
    ) -> None:

        if action is RemediationAction.TRIM:
            final_aliases = _ALIASES["confirm_trim"]
        elif action is RemediationAction.ERASE_SONG:
            final_aliases = _ALIASES["erase_song"]
        else:
            final_aliases = _ALIASES["confirm_mute"]
        final_buttons = self._visible_exact_locators("button", final_aliases)
        if not final_buttons and allow_save:
            final_buttons = self._visible_exact_locators("button", _ALIASES["save"])
        if len(final_buttons) != 1:
            raise StudioAmbiguousUi(
                f"Expected one final {action.value} confirmation, got {len(final_buttons)}"
            )
        final_buttons[0].click()

    def _accept_confirmation_checkbox(self) -> bool:
        """Accept the sole permanent-edit acknowledgement, when Studio shows it."""

        locator = self.page.get_by_role("checkbox")
        visible: list[Any] = []
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                visible.append(candidate)
        if len(visible) > 1:
            raise StudioAmbiguousUi(
                f"Expected at most one edit confirmation checkbox, got {len(visible)}"
            )
        if not visible:
            return False
        checkbox = visible[0]
        aria_disabled = checkbox.get_attribute("aria-disabled")
        aria_checked_before = checkbox.get_attribute("aria-checked")
        if aria_disabled == "true":
            raise StudioAutomationUnavailable(
                "The permanent-edit acknowledgement checkbox is disabled"
            )

        if aria_checked_before is not None:
            # YouTube uses a custom div[role=checkbox]. Locator.check() is only
            # appropriate for native inputs and waits for ten minutes here because
            # #checkbox-container intercepts pointer events. A DOM click invokes
            # the component's own handler without bypassing its state transition.
            if aria_checked_before != "true":
                checkbox.evaluate("element => element.click()")
                for _ in range(25):
                    if checkbox.get_attribute("aria-checked") == "true":
                        break
                    self.page.wait_for_timeout(200)
                else:
                    self.diagnostic.screenshot(
                        self.page, "permanent_edit_acknowledgement_failed"
                    )
                    raise StudioAutomationUnavailable(
                        "The permanent-edit acknowledgement did not become checked"
                    )
        elif not checkbox.is_checked():
            # Retain bounded compatibility with a native input checkbox.
            checkbox.check(timeout=5_000)

        aria_checked_after = checkbox.get_attribute("aria-checked")
        self.diagnostic.write_json(
            "permanent_edit_acknowledgement",
            {
                "aria_label": checkbox.get_attribute("aria-label"),
                "aria_checked_before": aria_checked_before,
                "aria_checked_after": aria_checked_after,
                "native_checked": (
                    checkbox.is_checked() if aria_checked_after is None else None
                ),
            },
        )
        logger.info("Accepted YouTube Studio permanent-edit acknowledgement")
        self.diagnostic.screenshot(self.page, "permanent_edit_acknowledged")
        return True

    def _wait_for_submitted_marker(self) -> None:
        try:
            self.page.wait_for_function(
                _SUBMISSION_CONFIRMED_SCRIPT,
                list(_SUBMITTED_MARKERS),
                timeout=30_000,
            )
        except Exception as exc:
            raise StudioSubmissionUncertain(
                "Studio did not confirm the result after the irreversible edit "
                "confirmation was clicked"
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
        return _visible_candidates(locator)

    def _visible_locators(self, role: str, aliases: Iterable[str]) -> list[Any]:
        locator = self.page.get_by_role(role, name=_pattern(aliases), exact=False)
        return _visible_candidates(locator)


def _pattern(aliases: Iterable[str]) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(value) for value in aliases), re.IGNORECASE)


def _visible_candidates(locator: Any) -> list[Any]:
    """Resolve visibility in one browser round trip, even for large claim lists."""

    try:
        indexes = locator.evaluate_all(_VISIBLE_INDEXES_SCRIPT)
        if isinstance(indexes, list) and all(
            isinstance(index, int) and index >= 0 for index in indexes
        ):
            return [locator.nth(index) for index in indexes]
    except Exception:
        logger.debug(
            "Batched Studio locator visibility check failed; using bounded fallback",
            exc_info=True,
        )

    result: list[Any] = []
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible():
            result.append(candidate)
    return result


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
