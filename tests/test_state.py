from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from state import (
    Platform,
    StateStore,
    StateStoreError,
    UploadPartSpec,
    UploadStatus,
)


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

        # Reconfirming success is idempotent.
        repeated = store.mark_success(video, "youtube", "yt-video-id")
        assert repeated.attempts == 1

    # sqlite3.Connection.__exit__ commits or rolls back but does not close.
    # Explicit closing is required on Windows so tmp_path is not left locked.
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
        with pytest.raises(StateStoreError, match="only for YouTube"):
            store.mark_playlist_added(video, Platform.CDA)


def test_store_rejects_operations_after_close(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.close()
    with pytest.raises(StateStoreError, match="is closed"):
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


def test_video_path_migration_preserves_all_platform_state(tmp_path: Path) -> None:
    old_video = tmp_path / "Stream 📻.mkv"
    new_video = tmp_path / "Stream.mkv"
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.mark_success(old_video, Platform.YOUTUBE, "yt-id")
        store.mark_failed(old_video, Platform.CDA, "HTTP 500")

        assert store.migrate_video_path(old_video, new_video) == 2
        assert store.get_status(old_video, Platform.YOUTUBE) is None
        assert store.get_status(new_video, Platform.YOUTUBE).platform_video_id == "yt-id"
        assert store.get_status(new_video, Platform.CDA).last_error == "HTTP 500"


def test_video_path_migration_refuses_target_state_conflict(tmp_path: Path) -> None:
    old_video = tmp_path / "old.mkv"
    new_video = tmp_path / "new.mkv"
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.mark_failed(old_video, Platform.CDA, "old")
        store.mark_failed(new_video, Platform.RUMBLE, "new")

        with pytest.raises(StateStoreError, match="target path"):
            store.migrate_video_path(old_video, new_video)

        assert store.get_status(old_video, Platform.CDA) is not None
        assert store.get_status(new_video, Platform.RUMBLE) is not None


def part_specs(tmp_path: Path, total: int = 2) -> list[UploadPartSpec]:
    specs: list[UploadPartSpec] = []
    for index in range(1, total + 1):
        part = tmp_path / f"part_{index:03d}.mkv"
        part.write_bytes(b"part")
        srt = tmp_path / f"part_{index:03d}_chat.srt"
        srt.write_text(
            f"1\n00:00:00,000 --> 00:00:01,000\npart {index}\n",
            encoding="utf-8",
        )
        specs.append(
            UploadPartSpec(
                index=index,
                total_parts=total,
                part_path=part,
                srt_path=srt,
                start_seconds=(index - 1) * 10,
                end_seconds=index * 10,
            )
        )
    return specs


def test_multipart_cycle_tracks_each_part_independently(tmp_path: Path) -> None:
    video = tmp_path / "stream.mkv"
    with StateStore(tmp_path / "state.sqlite3") as store:
        records = store.sync_upload_parts(
            video,
            Platform.YOUTUBE,
            part_specs(tmp_path),
        )
        assert [record.status for record in records] == [
            UploadStatus.PENDING,
            UploadStatus.PENDING,
        ]

        store.mark_part_in_progress(video, Platform.YOUTUBE, 1)
        first = store.mark_part_success(
            video,
            Platform.YOUTUBE,
            1,
            "youtube-part-1",
        )
        store.mark_part_captions_uploaded(video, Platform.YOUTUBE, 1)
        store.mark_part_playlist_added(video, Platform.YOUTUBE, 1)
        second = store.mark_part_failed(
            video,
            Platform.YOUTUBE,
            2,
            "network failure",
        )

        assert first.attempts == 1
        assert second.attempts == 1
        assert not store.are_parts_fully_processed(
            video,
            Platform.YOUTUBE,
            require_captions=True,
            require_playlist=True,
        )

        store.mark_part_in_progress(video, Platform.YOUTUBE, 2)
        store.mark_part_success(video, Platform.YOUTUBE, 2, "youtube-part-2")
        store.mark_part_captions_uploaded(video, Platform.YOUTUBE, 2)
        store.mark_part_playlist_added(video, Platform.YOUTUBE, 2)

        assert store.are_parts_fully_processed(
            video,
            Platform.YOUTUBE,
            require_captions=True,
            require_playlist=True,
        )
        final_records = store.get_part_statuses(video, Platform.YOUTUBE)
        assert [record.platform_video_id for record in final_records] == [
            "youtube-part-1",
            "youtube-part-2",
        ]


def test_sync_parts_preserves_terminal_status_and_rejects_manifest_change(
    tmp_path: Path,
) -> None:
    video = tmp_path / "stream.mkv"
    specs = part_specs(tmp_path)
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.sync_upload_parts(video, Platform.RUMBLE, specs)
        store.mark_part_success(video, Platform.RUMBLE, 1, "rumble-part-1")

        repeated = store.sync_upload_parts(video, Platform.RUMBLE, specs)
        assert repeated[0].status is UploadStatus.SUCCESS
        assert repeated[0].attempts == 1

        changed = list(specs)
        changed[0] = UploadPartSpec(
            index=1,
            total_parts=2,
            part_path=tmp_path / "replacement.mkv",
            start_seconds=0,
            end_seconds=8,
        )
        with pytest.raises(StateStoreError, match="terminal multipart part"):
            store.sync_upload_parts(video, Platform.RUMBLE, changed)


def test_empty_caption_part_does_not_block_multipart_completion(
    tmp_path: Path,
) -> None:
    video = tmp_path / "stream.mkv"
    specs = part_specs(tmp_path, total=1)
    specs[0].srt_path.write_text("", encoding="utf-8")
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.sync_upload_parts(video, Platform.YOUTUBE, specs)
        store.mark_part_success(video, Platform.YOUTUBE, 1, "youtube-part")
        store.mark_part_playlist_added(video, Platform.YOUTUBE, 1)

        assert store.are_parts_fully_processed(
            video,
            Platform.YOUTUBE,
            require_captions=True,
            require_playlist=True,
        )


def test_old_terminal_skip_can_be_reopened_for_multipart(tmp_path: Path) -> None:
    video = tmp_path / "stream.mkv"
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.mark_skipped(video, Platform.YOUTUBE, "over 12 hours")
        reopened = store.reopen_for_multipart(video, Platform.YOUTUBE)

        assert reopened.status is UploadStatus.PENDING
        assert reopened.last_error is None

        store.mark_success(video, Platform.YOUTUBE, "already-uploaded")
        with pytest.raises(StateStoreError, match="cannot be reopened"):
            store.reopen_for_multipart(video, Platform.YOUTUBE)


def test_video_path_migration_also_moves_multipart_records(tmp_path: Path) -> None:
    old_video = tmp_path / "old.mkv"
    new_video = tmp_path / "new.mkv"
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.sync_upload_parts(old_video, Platform.RUMBLE, part_specs(tmp_path))

        assert store.migrate_video_path(old_video, new_video) == 2
        assert store.get_part_statuses(old_video, Platform.RUMBLE) == []
        assert len(store.get_part_statuses(new_video, Platform.RUMBLE)) == 2
