from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from config import BrowserConfig, BrowserPlatformConfig, RetryConfig
from uploaders.cda import CDAUploader
from uploaders.rumble import RumbleUploader, _rumble_result_url, _set_category_by_label


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
    result = uploader.upload(tmp_path / "missing.mkv", "Tytul", "Opis", [])
    assert result.success is False
    assert "nie istnieje" in (result.error_message or "")


def test_rumble_requires_explicit_license_before_opening_browser(tmp_path: Path) -> None:
    video = tmp_path / "video.mkv"
    video.write_bytes(b"video")
    uploader = RumbleUploader(platform(tmp_path, "rumble"), browser(), retry())
    uploader._session_manager = object()

    result = uploader.upload(video, "Tytul", "Opis", [])

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

    result = uploader.upload(video, "Tytul", "Opis", [])

    assert result.success is False
    assert "limit Rumble" in (result.error_message or "")


class FakeLocator:
    def __init__(self, *, value=None, input_value="", href=None) -> None:
        self.value = value
        self.saved = None
        self._input_value = input_value
        self.href = href

    def count(self) -> int:
        return 1

    def evaluate_all(self, script, label):
        return self.value

    def evaluate(self, script, value):
        self.saved = value

    def input_value(self):
        return self._input_value

    def get_attribute(self, name):
        return self.href if name == "href" else None


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
