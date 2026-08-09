from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from youtube_copyright.models import (
    ActionState,
    ClaimType,
    CopyrightAction,
    CopyrightClaim,
    CopyrightVideo,
    RemediationAction,
    RestrictionKind,
    VideoState,
)
from youtube_copyright.state import CopyrightStateStore


def test_video_claim_action_and_run_lifecycle(tmp_path: Path) -> None:
    database = tmp_path / "data" / "state.sqlite3"
    with CopyrightStateStore(database) as store:
        store.start_run("run-1", "automatic")
        saved = store.upsert_video(
            CopyrightVideo(
                video_id="b7uH35WAR2U",
                state=VideoState.GLOBAL_BLOCKED,
                restriction_kind=RestrictionKind.GLOBAL,
                allowed_regions=(),
                restriction_reasons=("GLOBAL",),
                last_checked_at=datetime.now(timezone.utc),
            )
        )
        assert saved.video_id == "b7uH35WAR2U"
        assert saved.allowed_regions == ()

        claim = CopyrightClaim(
            fingerprint="claim-1",
            video_id=saved.video_id,
            claim_type=ClaimType.AUDIO,
            content_title="Test song",
            start_seconds=10,
            end_seconds=30,
            available_actions=(RemediationAction.ERASE_SONG, RemediationAction.MUTE_ALL),
            actionable=True,
        )
        assert store.replace_claims(saved.video_id, [claim]) == store.active_claims(
            saved.video_id
        )

        action = store.add_action(
            CopyrightAction(
                id=None,
                run_id="run-1",
                video_id=saved.video_id,
                claim_fingerprint=claim.fingerprint,
                action=RemediationAction.ERASE_SONG,
                state=ActionState.PLANNED,
            )
        )
        assert action.id is not None
        submitted = store.update_action(action.id, ActionState.SUBMITTED)
        assert submitted.submitted_at is not None
        assert store.latest_action(saved.video_id) == submitted

        store.finish_run(
            "run-1", status="SUCCESS", videos_checked=1, actions_submitted=1
        )

    with sqlite3.connect(database) as connection:
        run = connection.execute(
            "SELECT status, videos_checked FROM youtube_copyright_run"
        ).fetchone()
    assert run == ("SUCCESS", 1)


def test_replace_claims_resolves_claims_that_disappeared(tmp_path: Path) -> None:
    with CopyrightStateStore(tmp_path / "state.sqlite3") as store:
        store.upsert_video(CopyrightVideo(video_id="video123"))
        claim = CopyrightClaim(
            fingerprint="old-claim",
            video_id="video123",
            claim_type=ClaimType.VISUAL,
        )
        store.replace_claims("video123", [claim])
        assert len(store.active_claims("video123")) == 1
        store.replace_claims("video123", [])
        assert store.active_claims("video123") == []


def test_lists_only_due_videos(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    with CopyrightStateStore(tmp_path / "state.sqlite3") as store:
        store.upsert_video(
            CopyrightVideo(video_id="due", next_check_at=now - timedelta(seconds=1))
        )
        store.upsert_video(
            CopyrightVideo(video_id="later", next_check_at=now + timedelta(hours=1))
        )
        assert [video.video_id for video in store.list_due_videos(now)] == ["due"]


def test_reads_successful_publisher_video_ids(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE upload_status (
                video_path TEXT, platform TEXT, status TEXT, platform_video_id TEXT
            );
            CREATE TABLE upload_part_status (
                video_path TEXT, platform TEXT, status TEXT, platform_video_id TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO upload_status VALUES (?, 'youtube', 'SUCCESS', ?)",
            (r"E:\recordings\one.mkv", "video-one"),
        )
        connection.execute(
            "INSERT INTO upload_part_status VALUES (?, 'youtube', 'SUCCESS', ?)",
            (r"E:\recordings\two.mkv", "video-two"),
        )
        connection.execute(
            "INSERT INTO upload_status VALUES (?, 'youtube', 'SUCCESS', ?)",
            (
                r"E:\recordings\multipart.mkv",
                '[{"part":1,"id_or_url":"yi5ajmYIm6U"},'
                '{"part":2,"id_or_url":"https://youtu.be/3zf-FsrsKN8"}]',
            ),
        )
        connection.execute(
            "INSERT INTO upload_status VALUES (?, 'youtube', 'SUCCESS', ?)",
            (r"E:\recordings\invalid.mkv", "[invalid-json"),
        )
        connection.execute(
            "INSERT INTO upload_status VALUES (?, 'cda', 'SUCCESS', ?)",
            (r"E:\recordings\ignored.mkv", "cda-id"),
        )

    with CopyrightStateStore(database) as store:
        assert store.publisher_video_ids() == {
            "video-one": r"E:\recordings\one.mkv",
            "video-two": r"E:\recordings\two.mkv",
            "yi5ajmYIm6U": r"E:\recordings\multipart.mkv",
            "3zf-FsrsKN8": r"E:\recordings\multipart.mkv",
        }


def test_wal_is_enabled(tmp_path: Path) -> None:
    with CopyrightStateStore(tmp_path / "state.sqlite3") as store:
        mode = store._require_connection().execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_caption_backup_audit_record(tmp_path: Path) -> None:
    with CopyrightStateStore(tmp_path / "state.sqlite3") as store:
        store.start_run("run-caption", "automatic")
        store.upsert_video(CopyrightVideo(video_id="caption-video"))
        action = store.add_action(
            CopyrightAction(
                None,
                "run-caption",
                "caption-video",
                RemediationAction.TRIM,
                ActionState.PLANNED,
                trim_ranges=((10, 20),),
            )
        )
        original = tmp_path / "original.srt"
        record = store.save_caption_backup(
            action_id=action.id,
            video_id="caption-video",
            track_id="track",
            language="pl",
            name="Twitch Chat",
            original_path=original,
            original_duration_seconds=100,
            status="PENDING",
        )
        assert record.original_path == original
        updated = store.update_caption_backup(
            action.id, status="SERVING", adjusted_path=tmp_path / "adjusted.srt"
        )
        assert updated.status == "SERVING"
