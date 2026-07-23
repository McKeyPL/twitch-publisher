from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

import uploaders.cda as cda_module
from config import BrowserConfig, BrowserPlatformConfig, RetryConfig
from uploaders.cda import (
    CDAUploader,
    _cda_result_url,
    _clear_cda_stale_uploads,
    _find_cda_title_input,
    _find_cda_submit_button,
    _read_cda_upload_status,
    _set_checkbox_by_text,
    _set_radio_by_question,
    _wait_for_cda_result_url,
    _wait_for_cda_upload_complete,
)
from uploaders.base import UploadCancelled
from uploaders.browser_form import BrowserUploadError
from uploaders.rumble import (
    RumbleUploader,
    _accept_rumble_confirmation,
    _read_rumble_transfer_status,
    _rumble_result_url,
    _set_category_by_label,
    _wait_for_rumble_transfer,
)


def retry() -> RetryConfig:
    return RetryConfig(1, 0.01, 1, 0.01)


def browser() -> BrowserConfig:
    return BrowserConfig(None, True, False)


def platform(tmp_path: Path, name: str) -> BrowserPlatformConfig:
    return BrowserPlatformConfig(
        True, f"https://example.test/{name}", tmp_path / f"{name}.json", None, None
    )


def test_platform_names_and_playlist_support(tmp_path: Path) -> None:
    cda = CDAUploader(platform(tmp_path, "cda"), browser(), retry())
    rumble = RumbleUploader(platform(tmp_path, "rumble"), browser(), retry())
    assert cda.platform_name == "cda"
    assert rumble.platform_name == "rumble"
    assert cda.add_to_playlist("id", "collection") is False
    assert rumble.add_to_playlist("id", "collection") is False


def test_missing_video_returns_failure_without_opening_browser(tmp_path: Path) -> None:
    uploader = CDAUploader(platform(tmp_path, "cda"), browser(), retry())
    uploader._session_manager = object()
    result = uploader.upload(tmp_path / "missing.mkv", "Title", "Description", [])
    assert result.success is False
    assert "does not exist" in (result.error_message or "")


def test_rumble_requires_explicit_license_before_opening_browser(tmp_path: Path) -> None:
    video = tmp_path / "video.mkv"
    video.write_bytes(b"video")
    uploader = RumbleUploader(platform(tmp_path, "rumble"), browser(), retry())
    uploader._session_manager = object()

    result = uploader.upload(video, "Title", "Description", [])

    assert result.success is False
    assert "RUMBLE_LICENSE_OPTION" in (result.error_message or "")


def test_rumble_rejects_file_over_har_limit_before_browser(tmp_path: Path) -> None:
    video = tmp_path / "video.mkv"
    video.write_bytes(b"video")
    config = replace(
        platform(tmp_path, "rumble"),
        license_option="6",
        max_file_size_gb=0.000000001,
    )
    uploader = RumbleUploader(config, browser(), retry())
    uploader._session_manager = object()

    result = uploader.upload(video, "Title", "Description", [])

    assert result.success is False
    assert "exceeds the Rumble" in (result.error_message or "")


class FakeLocator:
    def __init__(self, *, value=None, input_value="", href=None) -> None:
        self.value = value
        self.saved = None
        self._input_value = input_value
        self.href = href

    def count(self) -> int:
        return 1

    def nth(self, index):
        assert index == 0
        return self

    def is_visible(self):
        return True

    def evaluate_all(self, script, label):
        return self.value

    def evaluate(self, script, value):
        self.saved = value

    def input_value(self):
        return self._input_value

    def get_attribute(self, name):
        return self.href if name == "href" else None

    def text_content(self):
        return self._input_value


class FakePage:
    def __init__(self, locators) -> None:
        self.locators = locators

    def locator(self, selector):
        return self.locators[selector]


def test_rumble_category_is_resolved_from_har_data_label() -> None:
    options = FakeLocator(value="6545")
    hidden = FakeLocator()
    page = FakePage(
        {
            ".select-option[data-value][data-label]": options,
            "#category_secondary": hidden,
        }
    )

    assert _set_category_by_label(page, "#category_secondary", "Deep Rock Galactic")
    assert hidden.saved == "6545"


def test_rumble_success_url_comes_from_form3_not_page_navigation() -> None:
    page = FakePage(
        {
            "#form3 textarea#direct": FakeLocator(
                input_value="https://rumble.com/vabc123-title.html"
            ),
            "#form3 p#view a[href]": FakeLocator(href=None),
        }
    )

    assert _rumble_result_url(page) == "https://rumble.com/vabc123-title.html"


def test_rumble_hidden_confirmation_uses_change_event_and_hidden_value() -> None:
    class Checkbox:
        checked = False

        def count(self):
            return 1

        def is_checked(self):
            return self.checked

        def evaluate(self, script):
            assert "new Event('change'" in script
            self.checked = True
            hidden.value = "1"

    class Hidden:
        value = "0"

        def count(self):
            return 1

        def input_value(self):
            return self.value

    checkbox = Checkbox()
    hidden = Hidden()

    class ConfirmationPage:
        def locator(self, selector):
            if selector == "#crights":
                return checkbox
            if selector == "#rights":
                return hidden
            raise AssertionError(f"Unexpected selector: {selector}")

    _accept_rumble_confirmation(ConfirmationPage(), "crights")

    assert checkbox.checked is True
    assert hidden.value == "1"


def test_rumble_transfer_waits_for_server_token_not_second_form() -> None:
    class TransferPage:
        def __init__(self):
            self.calls = 0
            self.waits = []

        def evaluate(self, script):
            assert "video[]" in script
            self.calls += 1
            if self.calls < 3:
                return {
                    "complete": False,
                    "failed": False,
                    "percent": self.calls * 10,
                    "details": [f"{self.calls * 10}% (3 MB/s)"],
                    "error": None,
                }
            return {
                "complete": True,
                "failed": False,
                "percent": 100,
                "details": ["100%"],
                "error": None,
            }

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = TransferPage()

    _wait_for_rumble_transfer(page, timeout_ms=10_000)

    assert page.calls == 3
    assert page.waits == [1000, 1000]


def test_rumble_transfer_status_requires_nonempty_video_token() -> None:
    class StatusPage:
        def evaluate(self, script):
            assert "videoToken" in script
            return {
                "complete": False,
                "failed": False,
                "percent": 100,
                "details": ["100%"],
                "error": None,
            }

    status = _read_rumble_transfer_status(StatusPage())

    assert status["percent"] == 100
    assert status["complete"] is False


def test_cancel_token_stops_before_opening_browser(tmp_path: Path) -> None:
    video = tmp_path / "video.mkv"
    video.write_bytes(b"video")
    cancel_event = Event()
    cancel_event.set()
    uploader = CDAUploader(
        platform(tmp_path, "cda"),
        browser(),
        retry(),
        cancel_event=cancel_event,
    )
    uploader._session_manager = object()

    with pytest.raises(UploadCancelled):
        uploader.upload(video, "Title", "Description", [])


class FakeEvaluatePage:
    def __init__(self) -> None:
        self.arguments = []

    def evaluate(self, script, argument):
        self.arguments.append(argument)
        return True


def test_cda_semantic_form_answers_are_forwarded() -> None:
    page = FakeEvaluatePage()

    assert _set_checkbox_by_text(page, "Akceptuje regulamin", True)
    assert _set_radio_by_question(page, "zawiera przemoc", False)

    assert page.arguments == [
        {"fragment": "Akceptuje regulamin", "checked": True},
        {"question": "zawiera przemoc", "answerYes": False},
    ]


def test_cda_upload_status_does_not_accept_ready_button_as_transfer_completion() -> None:
    class StatusPage:
        def evaluate(self, script):
            assert "dodaj do serwisu" in script
            assert "#uploader .fileListContainer .progress-bar" in script
            assert "transferStatusNode" in script
            assert "predkoscia" in script
            assert ".replace(/ł/g, 'l')" in script
            assert r"zosta[lł]\s+przes[lł]any" in script
            assert "complete: completeMarker || Boolean(duplicateMatch)" in script
            assert "completeMarker || Boolean(submit && !submit.disabled)" not in script
            return {
                "complete": False,
                "complete_marker": False,
                "submit_ready": True,
                "percent": 100,
                "transferred": "475 MB",
                "total": "475 MB",
                "speed": "108.82 mbit/s",
                "details": ["100%", "12 MB/s"],
            }

    status = _read_cda_upload_status(StatusPage())

    assert status["complete"] is False
    assert status["submit_ready"] is True
    assert status["percent"] == 100
    assert status["transferred"] == "475 MB"
    assert status["total"] == "475 MB"
    assert status["speed"] == "108.82 mbit/s"


def test_cda_trace_completion_text_is_explicitly_supported() -> None:
    class CompletedPage:
        def evaluate(self, script):
            assert "film zostal przeslany i oczekuje na publikacje" in script
            assert r"publikacj[eę]" in script
            return {
                "complete": True,
                "complete_marker": True,
                "submit_ready": True,
                "duplicate_url": None,
                "success_url": None,
                "percent": None,
                "transferred": None,
                "total": None,
                "speed": None,
                "transfer_text": None,
                "details": [
                    "Film został przesłany i oczekuje na publikację."
                ],
            }

    status = _read_cda_upload_status(CompletedPage())

    assert status["complete"] is True
    assert status["complete_marker"] is True


def test_cda_upload_wait_ignores_button_until_transfer_marker(monkeypatch) -> None:
    states = iter(
        (
            {
                "complete": False,
                "complete_marker": False,
                "submit_ready": True,
                "percent": None,
                "details": [],
            },
            {
                "complete": True,
                "complete_marker": True,
                "submit_ready": True,
                "percent": 100,
                "details": ["100%"],
            },
        )
    )
    monkeypatch.setattr(cda_module, "_read_cda_upload_status", lambda page: next(states))
    monkeypatch.setattr(cda_module.time, "sleep", lambda seconds: None)

    result = _wait_for_cda_upload_complete(object(), timeout_ms=10_000)

    assert result["complete_marker"] is True


def test_cda_publication_timeout_requires_manual_review(monkeypatch) -> None:
    class PendingPage:
        url = "https://www.cda.pl/uploader_video"

        def evaluate(self, script):
            return {
                "complete": True,
                "complete_marker": True,
                "submit_ready": True,
                "success_url": None,
                "duplicate_url": None,
                "percent": 100,
                "details": [],
            }

        def locator(self, selector):
            return CandidateList([])

    clock = [0.0]

    def monotonic() -> float:
        clock[0] += 0.02
        return clock[0]

    monkeypatch.setattr(cda_module.time, "monotonic", monotonic)
    monkeypatch.setattr(cda_module.time, "sleep", lambda seconds: None)

    with pytest.raises(BrowserUploadError) as caught:
        _wait_for_cda_result_url(PendingPage(), timeout_ms=10)

    assert caught.value.retriable is False
    assert caught.value.manual_review_required is True


def test_cda_duplicate_status_carries_existing_url() -> None:
    class StatusPage:
        def evaluate(self, script):
            assert "duplikatem" in script
            return {
                "complete": True,
                "complete_marker": False,
                "submit_ready": False,
                "duplicate_url": "https://www.cda.pl/video/3122407840",
                "percent": None,
                "details": [],
            }

    status = _read_cda_upload_status(StatusPage())

    assert status["duplicate_url"] == "https://www.cda.pl/video/3122407840"


def test_cda_stale_upload_cards_are_removed_before_new_file() -> None:
    class RemoveButton:
        def __init__(self, page) -> None:
            self.page = page

        def get_attribute(self, name):
            return "Usuń z listy." if name == "title" else None

        def evaluate(self, script):
            assert script == "element => element.click()"
            self.page.count -= 1

    class RemoveList:
        def __init__(self, page) -> None:
            self.page = page

        def count(self):
            return self.page.count

        @property
        def first(self):
            return RemoveButton(self.page)

    class RemovePage:
        def __init__(self) -> None:
            self.count = 2

        def locator(self, selector):
            assert "icon-remove-sign" in selector
            return RemoveList(self)

    page = RemovePage()

    assert _clear_cda_stale_uploads(page) == 2
    assert page.count == 0


def test_cda_current_add_to_service_button_is_supported() -> None:
    expected_selector = "button[type='button'][data-loading-text*='Dodaj do serwisu']"
    button = FakeLocator()

    class SubmitPage:
        def locator(self, selector):
            if selector == expected_selector:
                return button
            return CandidateList([])

    assert _find_cda_submit_button(SubmitPage()) is button


def test_cda_result_url_is_read_from_generated_dom_link() -> None:
    class ResultLink(FakeLocator):
        def input_value(self):
            raise AssertionError("input_value cannot be called for an <a> element")

    class ResultPage:
        url = "https://www.cda.pl/uploader_video"

        def __init__(self) -> None:
            self.selectors = []

        def locator(self, selector):
            self.selectors.append(selector)
            assert ".icon-file.icon-success" in selector
            if ".col-md-19:has" in selector:
                return ResultLink(href="/video/abc123")
            return CandidateList([])

    page = ResultPage()

    assert _cda_result_url(page) == "https://www.cda.pl/video/abc123"
    assert any(".col-md-19:has" in selector for selector in page.selectors)


class CandidateInput:
    def __init__(self, value: str, *, placeholder: str = "", identity: str = "") -> None:
        self.value = value
        self.placeholder = placeholder
        self.identity = identity

    def get_attribute(self, name):
        return {
            "placeholder": self.placeholder,
            "id": self.identity,
            "name": self.identity,
        }.get(name)

    def input_value(self):
        return self.value


class CandidateList:
    def __init__(self, items) -> None:
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    def is_visible(self):
        return bool(self.items)


class TitlePage:
    def __init__(self, candidates) -> None:
        self.candidates = candidates

    def locator(self, selector):
        if selector in ("#nazwa_wyswietlana", "input[name='nazwa_wyswietlana']"):
            return CandidateList([])
        return CandidateList(self.candidates)


def test_cda_title_field_is_selected_by_uploaded_filename() -> None:
    search = CandidateInput("", placeholder="Szukaj", identity="search")
    tags = CandidateInput("", placeholder="Tagi")
    title = CandidateInput("20260623 221513 buvanybu Torture room")
    page = TitlePage([search, tags, title])

    selected = _find_cda_title_input(
        page,
        Path("20260623_221513_buvanybu_Torture room.mkv"),
    )

    assert selected is title
