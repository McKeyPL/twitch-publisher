"""SQLite persistence for the standalone copyright guard."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Iterable

from .models import (
    ActionState,
    ClaimType,
    CopyrightAction,
    CopyrightClaim,
    CopyrightVideo,
    RemediationAction,
    RestrictionKind,
    VideoState,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS youtube_copyright_run (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    videos_checked INTEGER NOT NULL DEFAULT 0 CHECK (videos_checked >= 0),
    actions_submitted INTEGER NOT NULL DEFAULT 0 CHECK (actions_submitted >= 0),
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS youtube_copyright_video (
    video_id TEXT PRIMARY KEY,
    source_video_path TEXT,
    title TEXT,
    duration_seconds REAL CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    state TEXT NOT NULL,
    restriction_kind TEXT NOT NULL,
    allowed_regions_json TEXT,
    blocked_regions_json TEXT,
    restriction_reasons_json TEXT NOT NULL DEFAULT '[]',
    processing INTEGER NOT NULL DEFAULT 0 CHECK (processing IN (0, 1)),
    last_error TEXT,
    last_checked_at TEXT,
    next_check_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_youtube_copyright_video_due
    ON youtube_copyright_video(state, next_check_at);

CREATE TABLE IF NOT EXISTS youtube_copyright_claim (
    fingerprint TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES youtube_copyright_video(video_id) ON DELETE CASCADE,
    claim_type TEXT NOT NULL,
    content_title TEXT,
    claimant TEXT,
    start_seconds REAL CHECK (start_seconds IS NULL OR start_seconds >= 0),
    end_seconds REAL CHECK (end_seconds IS NULL OR end_seconds >= 0),
    available_actions_json TEXT NOT NULL DEFAULT '[]',
    actionable INTEGER NOT NULL DEFAULT 0 CHECK (actionable IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_youtube_copyright_claim_video
    ON youtube_copyright_claim(video_id, resolved_at);

CREATE TABLE IF NOT EXISTS youtube_copyright_action (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES youtube_copyright_run(run_id),
    video_id TEXT NOT NULL REFERENCES youtube_copyright_video(video_id),
    claim_fingerprint TEXT REFERENCES youtube_copyright_claim(fingerprint),
    action TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    error_message TEXT,
    trace_path TEXT,
    before_screenshot TEXT,
    confirmation_screenshot TEXT,
    after_screenshot TEXT,
    trim_ranges_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_youtube_copyright_action_video
    ON youtube_copyright_action(video_id, id);
CREATE INDEX IF NOT EXISTS idx_youtube_copyright_action_state
    ON youtube_copyright_action(state);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(value: datetime | None) -> str | None:
    return value.isoformat(timespec="microseconds") if value else None


def _deserialize(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _video_id(value: str) -> str:
    result = value.strip()
    if not result or len(result) > 32 or any(char.isspace() for char in result):
        raise ValueError(f"Invalid YouTube video ID: {value!r}")
    return result


class CopyrightStateStore:
    """Short-transaction copyright state sharing the publisher SQLite file."""

    def __init__(self, database_path: str | Path) -> None:
        raw_path = str(database_path).strip()
        if not raw_path:
            raise ValueError("database_path cannot be empty")
        if raw_path != ":memory:":
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection: sqlite3.Connection | None = sqlite3.connect(
            raw_path, timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def __enter__(self) -> "CopyrightStateStore":
        self._require_connection()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("CopyrightStateStore is closed")
        return self._connection

    def start_run(self, run_id: str, mode: str) -> None:
        now = _serialize(_now())
        with self._require_connection():
            self._require_connection().execute(
                """
                INSERT INTO youtube_copyright_run(run_id, mode, status, started_at)
                VALUES (?, ?, 'RUNNING', ?)
                """,
                (run_id.strip(), mode.strip(), now),
            )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        videos_checked: int = 0,
        actions_submitted: int = 0,
        error_message: str | None = None,
    ) -> None:
        with self._require_connection():
            cursor = self._require_connection().execute(
                """
                UPDATE youtube_copyright_run
                SET status = ?, finished_at = ?, videos_checked = ?,
                    actions_submitted = ?, error_message = ?
                WHERE run_id = ?
                """,
                (
                    status.strip(),
                    _serialize(_now()),
                    videos_checked,
                    actions_submitted,
                    error_message,
                    run_id.strip(),
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown copyright run {run_id!r}")

    def upsert_video(self, video: CopyrightVideo) -> CopyrightVideo:
        video_id = _video_id(video.video_id)
        now = _now()
        with self._require_connection():
            self._require_connection().execute(
                """
                INSERT INTO youtube_copyright_video(
                    video_id, source_video_path, title, duration_seconds, state,
                    restriction_kind, allowed_regions_json, blocked_regions_json,
                    restriction_reasons_json, processing, last_error,
                    last_checked_at, next_check_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    source_video_path = COALESCE(excluded.source_video_path, source_video_path),
                    title = COALESCE(excluded.title, title),
                    duration_seconds = COALESCE(excluded.duration_seconds, duration_seconds),
                    state = excluded.state,
                    restriction_kind = excluded.restriction_kind,
                    allowed_regions_json = excluded.allowed_regions_json,
                    blocked_regions_json = excluded.blocked_regions_json,
                    restriction_reasons_json = excluded.restriction_reasons_json,
                    processing = excluded.processing,
                    last_error = excluded.last_error,
                    last_checked_at = excluded.last_checked_at,
                    next_check_at = excluded.next_check_at,
                    updated_at = excluded.updated_at
                """,
                (
                    video_id,
                    video.source_video_path,
                    video.title,
                    video.duration_seconds,
                    video.state.value,
                    video.restriction_kind.value,
                    json.dumps(video.allowed_regions) if video.allowed_regions is not None else None,
                    json.dumps(video.blocked_regions) if video.blocked_regions is not None else None,
                    json.dumps(video.restriction_reasons),
                    int(video.processing),
                    video.last_error,
                    _serialize(video.last_checked_at),
                    _serialize(video.next_check_at),
                    _serialize(video.created_at or now),
                    _serialize(now),
                ),
            )
        record = self.get_video(video_id)
        assert record is not None
        return record

    def get_video(self, video_id: str) -> CopyrightVideo | None:
        row = self._require_connection().execute(
            "SELECT * FROM youtube_copyright_video WHERE video_id = ?",
            (_video_id(video_id),),
        ).fetchone()
        return self._video_from_row(row) if row else None

    def list_due_videos(self, now: datetime | None = None) -> list[CopyrightVideo]:
        moment = _serialize(now or _now())
        rows = self._require_connection().execute(
            """
            SELECT * FROM youtube_copyright_video
            WHERE next_check_at IS NULL OR next_check_at <= ?
            ORDER BY COALESCE(next_check_at, created_at), video_id
            """,
            (moment,),
        ).fetchall()
        return [self._video_from_row(row) for row in rows]

    def publisher_video_ids(self) -> dict[str, str | None]:
        """Return successful YouTube IDs and their source paths from publisher tables."""
        connection = self._require_connection()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        result: dict[str, str | None] = {}
        if "upload_status" in tables:
            rows = connection.execute(
                """
                SELECT platform_video_id, video_path FROM upload_status
                WHERE platform = 'youtube' AND status = 'SUCCESS'
                  AND platform_video_id IS NOT NULL
                """
            ).fetchall()
            for row in rows:
                result[_video_id(row["platform_video_id"])] = row["video_path"]
        if "upload_part_status" in tables:
            rows = connection.execute(
                """
                SELECT platform_video_id, video_path FROM upload_part_status
                WHERE platform = 'youtube' AND status = 'SUCCESS'
                  AND platform_video_id IS NOT NULL
                """
            ).fetchall()
            for row in rows:
                result[_video_id(row["platform_video_id"])] = row["video_path"]
        return result

    def replace_claims(
        self, video_id: str, claims: Iterable[CopyrightClaim]
    ) -> list[CopyrightClaim]:
        normalized_id = _video_id(video_id)
        claim_list = list(claims)
        now = _now()
        fingerprints = {claim.fingerprint for claim in claim_list}
        with self._require_connection():
            for claim in claim_list:
                if claim.video_id != normalized_id:
                    raise ValueError("claim video_id does not match the target video")
                self._require_connection().execute(
                    """
                    INSERT INTO youtube_copyright_claim(
                        fingerprint, video_id, claim_type, content_title, claimant,
                        start_seconds, end_seconds, available_actions_json, actionable,
                        first_seen_at, last_seen_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        claim_type = excluded.claim_type,
                        content_title = excluded.content_title,
                        claimant = excluded.claimant,
                        start_seconds = excluded.start_seconds,
                        end_seconds = excluded.end_seconds,
                        available_actions_json = excluded.available_actions_json,
                        actionable = excluded.actionable,
                        last_seen_at = excluded.last_seen_at,
                        resolved_at = NULL
                    """,
                    (
                        claim.fingerprint,
                        normalized_id,
                        claim.claim_type.value,
                        claim.content_title,
                        claim.claimant,
                        claim.start_seconds,
                        claim.end_seconds,
                        json.dumps([action.value for action in claim.available_actions]),
                        int(claim.actionable),
                        _serialize(claim.first_seen_at or now),
                        _serialize(now),
                    ),
                )
            if fingerprints:
                placeholders = ",".join("?" for _ in fingerprints)
                self._require_connection().execute(
                    f"""
                    UPDATE youtube_copyright_claim SET resolved_at = ?
                    WHERE video_id = ? AND resolved_at IS NULL
                      AND fingerprint NOT IN ({placeholders})
                    """,
                    (_serialize(now), normalized_id, *sorted(fingerprints)),
                )
            else:
                self._require_connection().execute(
                    """
                    UPDATE youtube_copyright_claim SET resolved_at = ?
                    WHERE video_id = ? AND resolved_at IS NULL
                    """,
                    (_serialize(now), normalized_id),
                )
        return self.active_claims(normalized_id)

    def active_claims(self, video_id: str) -> list[CopyrightClaim]:
        rows = self._require_connection().execute(
            """
            SELECT * FROM youtube_copyright_claim
            WHERE video_id = ? AND resolved_at IS NULL
            ORDER BY COALESCE(start_seconds, 0), fingerprint
            """,
            (_video_id(video_id),),
        ).fetchall()
        return [self._claim_from_row(row) for row in rows]

    def add_action(self, action: CopyrightAction) -> CopyrightAction:
        now = action.created_at or _now()
        with self._require_connection():
            cursor = self._require_connection().execute(
                """
                INSERT INTO youtube_copyright_action(
                    run_id, video_id, claim_fingerprint, action, state, attempt,
                    error_message, trace_path, before_screenshot,
                    confirmation_screenshot, after_screenshot, trim_ranges_json,
                    created_at, submitted_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.run_id,
                    _video_id(action.video_id),
                    action.claim_fingerprint,
                    action.action.value,
                    action.state.value,
                    action.attempt,
                    action.error_message,
                    action.trace_path,
                    action.before_screenshot,
                    action.confirmation_screenshot,
                    action.after_screenshot,
                    json.dumps(action.trim_ranges),
                    _serialize(now),
                    _serialize(action.submitted_at),
                    _serialize(action.completed_at),
                ),
            )
        result = self.get_action(int(cursor.lastrowid))
        assert result is not None
        return result

    def update_action(
        self,
        action_id: int,
        state: ActionState,
        *,
        error_message: str | None = None,
        trace_path: str | None = None,
        before_screenshot: str | None = None,
        confirmation_screenshot: str | None = None,
        after_screenshot: str | None = None,
    ) -> CopyrightAction:
        submitted = _serialize(_now()) if state is ActionState.SUBMITTED else None
        completed = (
            _serialize(_now())
            if state in {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELLED}
            else None
        )
        with self._require_connection():
            cursor = self._require_connection().execute(
                """
                UPDATE youtube_copyright_action SET
                    state = ?, error_message = ?,
                    trace_path = COALESCE(?, trace_path),
                    before_screenshot = COALESCE(?, before_screenshot),
                    confirmation_screenshot = COALESCE(?, confirmation_screenshot),
                    after_screenshot = COALESCE(?, after_screenshot),
                    submitted_at = COALESCE(?, submitted_at),
                    completed_at = COALESCE(?, completed_at)
                WHERE id = ?
                """,
                (
                    state.value,
                    error_message,
                    trace_path,
                    before_screenshot,
                    confirmation_screenshot,
                    after_screenshot,
                    submitted,
                    completed,
                    action_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown copyright action {action_id}")
        result = self.get_action(action_id)
        assert result is not None
        return result

    def get_action(self, action_id: int) -> CopyrightAction | None:
        row = self._require_connection().execute(
            "SELECT * FROM youtube_copyright_action WHERE id = ?", (action_id,)
        ).fetchone()
        return self._action_from_row(row) if row else None

    def latest_action(self, video_id: str) -> CopyrightAction | None:
        row = self._require_connection().execute(
            """
            SELECT * FROM youtube_copyright_action
            WHERE video_id = ? ORDER BY id DESC LIMIT 1
            """,
            (_video_id(video_id),),
        ).fetchone()
        return self._action_from_row(row) if row else None

    @staticmethod
    def _video_from_row(row: sqlite3.Row) -> CopyrightVideo:
        allowed = json.loads(row["allowed_regions_json"]) if row["allowed_regions_json"] else None
        blocked = json.loads(row["blocked_regions_json"]) if row["blocked_regions_json"] else None
        return CopyrightVideo(
            video_id=row["video_id"],
            source_video_path=row["source_video_path"],
            title=row["title"],
            duration_seconds=row["duration_seconds"],
            state=VideoState(row["state"]),
            restriction_kind=RestrictionKind(row["restriction_kind"]),
            allowed_regions=tuple(allowed) if allowed is not None else None,
            blocked_regions=tuple(blocked) if blocked is not None else None,
            restriction_reasons=tuple(json.loads(row["restriction_reasons_json"])),
            processing=bool(row["processing"]),
            last_error=row["last_error"],
            last_checked_at=_deserialize(row["last_checked_at"]),
            next_check_at=_deserialize(row["next_check_at"]),
            created_at=_deserialize(row["created_at"]),
            updated_at=_deserialize(row["updated_at"]),
        )

    @staticmethod
    def _claim_from_row(row: sqlite3.Row) -> CopyrightClaim:
        return CopyrightClaim(
            fingerprint=row["fingerprint"],
            video_id=row["video_id"],
            claim_type=ClaimType(row["claim_type"]),
            content_title=row["content_title"],
            claimant=row["claimant"],
            start_seconds=row["start_seconds"],
            end_seconds=row["end_seconds"],
            available_actions=tuple(
                RemediationAction(value)
                for value in json.loads(row["available_actions_json"])
            ),
            actionable=bool(row["actionable"]),
            first_seen_at=_deserialize(row["first_seen_at"]),
            last_seen_at=_deserialize(row["last_seen_at"]),
            resolved_at=_deserialize(row["resolved_at"]),
        )

    @staticmethod
    def _action_from_row(row: sqlite3.Row) -> CopyrightAction:
        ranges = json.loads(row["trim_ranges_json"])
        return CopyrightAction(
            id=row["id"],
            run_id=row["run_id"],
            video_id=row["video_id"],
            claim_fingerprint=row["claim_fingerprint"],
            action=RemediationAction(row["action"]),
            state=ActionState(row["state"]),
            attempt=row["attempt"],
            error_message=row["error_message"],
            trace_path=row["trace_path"],
            before_screenshot=row["before_screenshot"],
            confirmation_screenshot=row["confirmation_screenshot"],
            after_screenshot=row["after_screenshot"],
            trim_ranges=tuple((float(start), float(end)) for start, end in ranges),
            created_at=_deserialize(row["created_at"]),
            submitted_at=_deserialize(row["submitted_at"]),
            completed_at=_deserialize(row["completed_at"]),
        )
