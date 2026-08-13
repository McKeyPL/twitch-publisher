from __future__ import annotations

import re
from pathlib import Path

import pytest

from config import CopyrightDiagnosticsConfig
from youtube_copyright.diagnostics import DiagnosticRun
from youtube_copyright.models import RemediationAction
from youtube_copyright.studio_executor import (
    StudioAmbiguousUi,
    StudioAutomationUnavailable,
    StudioCopyrightExecutor,
)
from youtube_copyright.studio_parser import StudioClaimParser, _CLAIM_EXTRACTION_SCRIPT


class FakeElement:
    def __init__(
        self,
        label: str,
        on_click=None,
        *,
        aria_checked: str | None = None,
    ) -> None:
        self.label = label
        self.clicks = 0
        self.checked = False
        self.check_calls = 0
        self.on_click = on_click
        self.aria_checked = aria_checked

    def click(self, **kwargs: object) -> None:
        self.clicks += 1
        if self.on_click is not None:
            self.on_click()

    def is_visible(self) -> bool:
        return True

    def is_checked(self) -> bool:
        return self.checked

    def check(self, **kwargs: object) -> None:
        self.check_calls += 1
        self.checked = True

    def evaluate(self, script: str) -> str:
        if "element.click()" in script:
            self.click()
            self.checked = True
            self.aria_checked = "true"
            return ""
        return f"<{self.label}>"

    def get_attribute(self, name: str) -> str | None:
        if name == "aria-checked":
            return self.aria_checked
        if name == "aria-disabled":
            return "false"
        if name == "aria-label":
            return self.label
        return None


class FakeLocatorList:
    def __init__(self, elements: list[FakeElement]) -> None:
        self.elements = elements

    def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> FakeElement:
        return self.elements[index]


class FakeMarker:
    @property
    def first(self):
        return self

    def wait_for(self, **kwargs: object) -> None:
        return None


class FakeResponse:
    url = "https://studio.youtube.com/youtubei/v1/creator/list_creator_received_claims"
    status = 200


class FakeResponseInfo:
    def __init__(self, page: "FakePage") -> None:
        self.page = page
        self.value = FakeResponse()

    def __enter__(self) -> "FakeResponseInfo":
        self.page.events.append("expect_response_enter")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.page.events.append("expect_response_exit")
        if self.page.response_error is not None:
            raise self.page.response_error


class FakeBody:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self, **kwargs: object) -> str:
        return self.text


class FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class FakePage:
    def __init__(self, tmp_path: Path, roles: dict[str, list[str]]) -> None:
        self.tmp_path = tmp_path
        self.url = "https://studio.youtube.com/video/video123/copyright?hl=en"
        self.roles = {
            role: [
                FakeElement(
                    label,
                    aria_checked="false" if role == "checkbox" else None,
                )
                for label in labels
            ]
            for role, labels in roles.items()
        }
        self.keyboard = FakeKeyboard()
        self.rows: list[dict[str, str]] = []
        self.body_text = ""
        self.events: list[str] = []
        self.response_error: Exception | None = None

    def expect_response(self, predicate, **kwargs: object) -> FakeResponseInfo:
        assert predicate(FakeResponse())
        assert kwargs["timeout"] == 30_000
        return FakeResponseInfo(self)

    def goto(self, url: str, **kwargs: object) -> None:
        self.events.append("goto")
        self.url = url

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.events.append(f"wait:{milliseconds}")
        return None

    def wait_for_function(self, script: str, **kwargs: object) -> None:
        assert "ytcr-video-content-list-row" in script
        assert kwargs["timeout"] == 10_000
        self.events.append("wait_for_claim_ui")

    def screenshot(self, path: str, **kwargs: object) -> None:
        Path(path).write_bytes(b"png")

    def evaluate(self, script: str):
        return self.rows

    def locator(self, selector: str):
        assert selector == "body"
        return FakeBody(self.body_text)

    def get_by_role(
        self,
        role: str,
        *,
        name: re.Pattern[str] | None = None,
        exact: bool | None = None,
    ):
        return FakeLocatorList(
            [
                element
                for element in self.roles.get(role, [])
                if name is None or name.search(element.label)
            ]
        )

    def get_by_text(self, pattern: re.Pattern[str], exact: bool):
        return FakeMarker()


def _diagnostic(tmp_path: Path) -> DiagnosticRun:
    return DiagnosticRun(CopyrightDiagnosticsConfig(tmp_path / "logs", 14), "run", "video123")


def test_inspection_extracts_claims_and_processing_state(tmp_path: Path) -> None:
    page = FakePage(tmp_path, {})
    page.rows = [
        {
            "text": "Song\nSound recording\n00:10 - 00:20",
            "actions": "Erase song",
        }
    ]
    page.body_text = "Video editing is in progress"
    inspection = StudioCopyrightExecutor(page, _diagnostic(tmp_path)).inspect("video123")
    assert inspection.processing
    assert len(inspection.claims) == 1
    assert inspection.claims[0].claim.start_seconds == 10
    assert page.url.endswith("/video/video123/claims?hl=en")
    assert page.events[:4] == [
        "expect_response_enter",
        "goto",
        "expect_response_exit",
        "wait:750",
    ]
    assert "wait_for_claim_ui" in page.events


def test_inspection_recognizes_english_processing_screen(tmp_path: Path) -> None:
    page = FakePage(tmp_path, {})
    page.body_text = (
        "Video editing is in progress... This process might take a while. "
        "Still processing"
    )

    inspection = StudioCopyrightExecutor(page, _diagnostic(tmp_path)).inspect(
        "video123"
    )

    assert inspection.processing
    assert inspection.claims == ()


def test_inspection_refuses_to_treat_unfinished_claim_request_as_empty(
    tmp_path: Path,
) -> None:
    page = FakePage(tmp_path, {})
    page.response_error = TimeoutError("claim request timed out")
    with pytest.raises(StudioAutomationUnavailable, match="did not finish loading"):
        StudioCopyrightExecutor(page, _diagnostic(tmp_path)).inspect("video123")


def test_parser_supports_new_youtube_claim_row_component() -> None:
    assert "ytcr-video-content-list-row" in _CLAIM_EXTRACTION_SCRIPT


def test_dry_run_opens_audio_action_but_does_not_confirm(tmp_path: Path) -> None:
    page = FakePage(
        tmp_path,
        {"button": ["Take action"], "menuitem": ["Erase song"]},
    )
    parsed = StudioClaimParser().parse_rows(
        "video123",
        [{"text": "Song\nAudio\n00:10 - 00:20", "actions": "Erase song"}],
    )[0]
    result = StudioCopyrightExecutor(page, _diagnostic(tmp_path)).execute(
        "video123",
        parsed,
        RemediationAction.ERASE_SONG,
        dry_run=True,
        trace_path=None,
    )
    assert not result.submitted
    assert result.dry_run
    assert page.roles["button"][0].clicks == 1
    assert page.roles["menuitem"][0].clicks == 1
    assert page.keyboard.pressed == ["Escape"]


def test_dry_run_accepts_english_claims_ui_actions_button(tmp_path: Path) -> None:
    page = FakePage(
        tmp_path,
        {"button": ["Take action"], "menuitem": ["Erase song"]},
    )
    parsed = StudioClaimParser().parse_rows(
        "video123",
        [{"text": "Song\nAudio\n00:10 - 00:20", "actions": ""}],
    )[0]
    result = StudioCopyrightExecutor(page, _diagnostic(tmp_path)).execute(
        "video123",
        parsed,
        RemediationAction.ERASE_SONG,
        dry_run=True,
        trace_path=None,
    )
    assert result.dry_run
    assert page.roles["button"][0].clicks == 1
    assert page.roles["menuitem"][0].clicks == 1


def test_automatic_trim_requires_and_clicks_one_confirmation(tmp_path: Path) -> None:
    page = FakePage(
        tmp_path,
        {
            "button": ["Take action", "Trim"],
            "menuitem": ["Trim out segment"],
        },
    )
    parsed = StudioClaimParser().parse_rows(
        "video123",
        [{"text": "Visual video segment\n00:10 - 00:20", "actions": "Trim out segment"}],
    )[0]
    result = StudioCopyrightExecutor(page, _diagnostic(tmp_path)).execute(
        "video123",
        parsed,
        RemediationAction.TRIM,
        dry_run=False,
        trace_path=tmp_path / "trace.zip",
    )
    assert result.submitted
    assert page.roles["button"][1].clicks == 1


def test_automatic_erase_accepts_permanent_edit_checkbox(tmp_path: Path) -> None:
    page = FakePage(
        tmp_path,
        {
            "button": ["Take action", "Continue", "Erase song"],
            "menuitem": ["Erase song"],
            "checkbox": ["I understand this edit is permanent"],
        },
    )
    parsed = StudioClaimParser().parse_rows(
        "video123",
        [{"text": "Song\nAudio", "actions": ""}],
    )[0]
    result = StudioCopyrightExecutor(page, _diagnostic(tmp_path)).execute(
        "video123",
        parsed,
        RemediationAction.ERASE_SONG,
        dry_run=False,
        trace_path=None,
    )
    assert result.submitted
    assert page.roles["button"][1].clicks == 1
    assert page.roles["checkbox"][0].checked
    assert page.roles["checkbox"][0].check_calls == 0
    assert page.roles["button"][2].clicks == 1


def test_current_erase_flow_saves_before_permanent_edit_confirmation(
    tmp_path: Path,
) -> None:
    page = FakePage(
        tmp_path,
        {
            "button": ["Take action", "Continue"],
            "menuitem": ["Erase song"],
        },
    )
    take_action, continue_button = page.roles["button"]
    checkbox = FakeElement(
        "I acknowledge that these changes are permanent",
        aria_checked="false",
    )
    confirm_changes = FakeElement("Confirm changes")

    def show_permanent_edit_dialog() -> None:
        page.roles["button"] = [take_action, confirm_changes]
        page.roles["checkbox"] = [checkbox]

    save_button = FakeElement("Save", on_click=show_permanent_edit_dialog)
    page.roles["button"].append(save_button)
    parsed = StudioClaimParser().parse_rows(
        "video123",
        [{"text": "Song\nAudio", "actions": ""}],
    )[0]

    result = StudioCopyrightExecutor(page, _diagnostic(tmp_path)).execute(
        "video123",
        parsed,
        RemediationAction.ERASE_SONG,
        dry_run=False,
        trace_path=None,
    )

    assert result.submitted
    assert continue_button.clicks == 1
    assert save_button.clicks == 1
    assert checkbox.checked
    assert checkbox.aria_checked == "true"
    assert checkbox.check_calls == 0
    assert confirm_changes.clicks == 1


def test_executor_refuses_wrong_video_page(tmp_path: Path) -> None:
    page = FakePage(tmp_path, {})
    page.url = "https://studio.youtube.com/video/other/copyright"
    parsed = StudioClaimParser().parse_rows(
        "video123", [{"text": "Song Audio", "actions": "Erase song"}]
    )[0]
    with pytest.raises(StudioAmbiguousUi, match="expected video"):
        StudioCopyrightExecutor(page, _diagnostic(tmp_path)).execute(
            "video123",
            parsed,
            RemediationAction.ERASE_SONG,
            dry_run=True,
            trace_path=None,
        )
