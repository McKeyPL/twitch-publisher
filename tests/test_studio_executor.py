from __future__ import annotations

import re
from pathlib import Path

import pytest

from config import CopyrightDiagnosticsConfig
from youtube_copyright.diagnostics import DiagnosticRun
from youtube_copyright.models import RemediationAction
from youtube_copyright.studio_executor import (
    StudioAmbiguousUi,
    StudioCopyrightExecutor,
)
from youtube_copyright.studio_parser import StudioClaimParser


class FakeElement:
    def __init__(self, label: str) -> None:
        self.label = label
        self.clicks = 0

    def click(self) -> None:
        self.clicks += 1

    def is_visible(self) -> bool:
        return True

    def evaluate(self, script: str) -> str:
        return f"<{self.label}>"


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
            role: [FakeElement(label) for label in labels] for role, labels in roles.items()
        }
        self.keyboard = FakeKeyboard()
        self.rows: list[dict[str, str]] = []
        self.body_text = ""

    def goto(self, url: str, **kwargs: object) -> None:
        self.url = url

    def wait_for_timeout(self, milliseconds: int) -> None:
        return None

    def screenshot(self, path: str, **kwargs: object) -> None:
        Path(path).write_bytes(b"png")

    def evaluate(self, script: str):
        return self.rows

    def locator(self, selector: str):
        assert selector == "body"
        return FakeBody(self.body_text)

    def get_by_role(self, role: str, *, name: re.Pattern[str], exact: bool):
        return FakeLocatorList(
            [element for element in self.roles.get(role, []) if name.search(element.label)]
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
