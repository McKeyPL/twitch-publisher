from __future__ import annotations

from pathlib import Path

from config import BrowserConfig, BrowserPlatformConfig, RetryConfig
from uploaders.cda import CDAUploader
from uploaders.rumble import RumbleUploader


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
