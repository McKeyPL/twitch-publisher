from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from state import Platform, StateStore, StateStoreError, UploadStatus


def test_pending_in_progress_success_cycle_and_wal(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "upload_state.sqlite3"
    video = tmp_path / "stream.mkv"

    with StateStore(database) as store:
        pending = store.get_or_create_status(video, Platform.YOUTUBE)
        assert pending.status is UploadStatus.PENDING
        assert pending.attempts == 0
        assert pending.video_path.is_absolute()
        assert database.parent.is_dir()

        in_progress = store.mark_in_progress(video, "youtube")
        assert in_progress.status is UploadStatus.IN_PROGRESS
        assert in_progress.attempts == 0

        success = store.mark_success(video, "youtube", "yt-video-id")
        assert success.status is UploadStatus.SUCCESS
        assert success.platform_video_id == "yt-video-id"
        assert success.attempts == 1
        assert success.updated_at >= success.created_at

        # Powtorne potwierdzenie sukcesu jest idempotentne.
        repeated = store.mark_success(video, "youtube", "yt-video-id")
        assert repeated.attempts == 1

    # sqlite3.Connection.__exit__ robi commit/rollback, ale nie zamyka polaczenia.
    # closing jest konieczny na Windows, aby tmp_path nie pozostal zablokowany.
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_failed_attempts_increment_on_every_failure(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    video = tmp_path / "stream.mkv"

    with StateStore(database) as store:
        first = store.mark_failed(video, Platform.CDA, "timeout")
        assert first.status is UploadStatus.FAILED
        assert first.attempts == 1
        assert first.last_error == "timeout"

        store.mark_in_progress(video, Platform.CDA)
        second = store.mark_failed(video, Platform.CDA, "HTTP 500")
        assert second.attempts == 2
        assert second.last_error == "HTTP 500"


def test_fully_processed_accepts_success_and_legal_skip(tmp_path: Path) -> None:
    video = tmp_path / "stream-over-12h.mkv"
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.mark_skipped(video, Platform.YOUTUBE, "Material przekracza 12 godzin")
        store.mark_success(video, Platform.CDA, "https://cda.pl/video/123")
        store.mark_success(video, Platform.RUMBLE, "https://rumble.com/v123")

        assert store.is_fully_processed(
            video, ["youtube", "cda", "rumble"]
        )


def test_fully_processed_is_false_when_one_platform_failed(tmp_path: Path) -> None:
    video = tmp_path / "stream.mkv"
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.mark_success(video, Platform.YOUTUBE, "yt-id")
        store.mark_success(video, Platform.CDA, "cda-url")
        store.mark_failed(video, Platform.RUMBLE, "network error")

        assert not store.is_fully_processed(video, ["youtube", "cda", "rumble"])


def test_captions_and_playlist_flags_are_independent(tmp_path: Path) -> None:
    video = tmp_path / "stream.mkv"
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.mark_success(video, Platform.YOUTUBE, "yt-id")
        captions = store.mark_captions_uploaded(video, Platform.YOUTUBE)
        assert captions.captions_uploaded is True
        assert captions.playlist_added is False

        playlist = store.mark_playlist_added(video, Platform.YOUTUBE)
        assert playlist.captions_uploaded is True
        assert playlist.playlist_added is True

        store.mark_success(video, Platform.CDA, "cda-url")
        with pytest.raises(StateStoreError, match="tylko dla YouTube"):
            store.mark_playlist_added(video, Platform.CDA)


def test_store_rejects_operations_after_close(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.close()
    with pytest.raises(StateStoreError, match="zamkniety"):
        store.get_status(tmp_path / "stream.mkv", Platform.YOUTUBE)


def test_quota_reservation_below_limit(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        reserved, used = store.try_reserve_quota(
            "youtube_general", "2026-07-17", 400, 10_000
        )
        assert reserved is True
        assert used == 400
        assert store.get_quota_usage("youtube_general", "2026-07-17") == 400


def test_quota_reservation_refuses_limit_overflow_atomically(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        assert store.try_reserve_quota(
            "youtube_videos_insert", "2026-07-17", 99, 100
        ) == (True, 99)
        assert store.try_reserve_quota(
            "youtube_videos_insert", "2026-07-17", 2, 100
        ) == (False, 99)
        assert store.get_quota_usage("youtube_videos_insert", "2026-07-17") == 99


def test_quota_period_change_starts_new_counter(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        assert store.try_reserve_quota(
            "youtube_videos_insert", "2026-07-17", 100, 100
        ) == (True, 100)
        assert store.try_reserve_quota(
            "youtube_videos_insert", "2026-07-18", 1, 100
        ) == (True, 1)
        assert store.get_quota_usage("youtube_videos_insert", "2026-07-17") == 100
        assert store.get_quota_usage("youtube_videos_insert", "2026-07-18") == 1
