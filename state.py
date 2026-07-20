"""Persistent per-platform recording publication state in SQLite."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Iterable


class StateStoreError(RuntimeError):
    """An upload-state operation cannot be completed."""


class Platform(str, Enum):
    YOUTUBE = "youtube"
    CDA = "cda"
    RUMBLE = "rumble"


class UploadStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class UploadStatusRecord:
    video_path: Path
    platform: Platform
    status: UploadStatus
    platform_video_id: str | None
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    captions_uploaded: bool
    playlist_added: bool


_SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_status (
    video_path TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('youtube', 'cda', 'rumble')),
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'IN_PROGRESS', 'SUCCESS', 'SKIPPED', 'FAILED')
    ),
    platform_video_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    captions_uploaded INTEGER NOT NULL DEFAULT 0 CHECK (captions_uploaded IN (0, 1)),
    playlist_added INTEGER NOT NULL DEFAULT 0 CHECK (playlist_added IN (0, 1)),
    PRIMARY KEY (video_path, platform)
);

CREATE INDEX IF NOT EXISTS idx_upload_status_video_path
    ON upload_status(video_path);
CREATE INDEX IF NOT EXISTS idx_upload_status_status
    ON upload_status(status);

CREATE TABLE IF NOT EXISTS api_quota_usage (
    platform TEXT NOT NULL,
    quota_period TEXT NOT NULL,
    units INTEGER NOT NULL DEFAULT 0 CHECK (units >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (platform, quota_period)
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_datetime(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _deserialize_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _coerce_platform(platform: str | Platform) -> Platform:
    if isinstance(platform, Platform):
        return platform
    if not isinstance(platform, str):
        raise StateStoreError("platform must be a string or Platform value")
    try:
        return Platform(platform.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in Platform)
        raise StateStoreError(f"Unknown platform {platform!r}; allowed values: {allowed}") from exc


def _normalize_video_path(video_path: str | Path) -> Path:
    if not isinstance(video_path, (str, Path)):
        raise StateStoreError("video_path must be path-like")
    raw = str(video_path).strip()
    if not raw:
        raise StateStoreError("video_path cannot be empty")
    if "\x00" in raw:
        raise StateStoreError("video_path contains a forbidden NUL character")

    absolute = Path(raw).expanduser().resolve(strict=False)
    # normcase prevents duplicates that differ only by letter case on Windows.
    return Path(os.path.normcase(str(absolute)))


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the current schema; safe to call whenever the database opens."""
    connection.executescript(_SCHEMA)
    connection.commit()


class StateStore:
    """One long-lived SQLite connection for the publisher process."""

    def __init__(self, database_path: str | Path) -> None:
        raw_path = str(database_path).strip()
        if not raw_path:
            raise StateStoreError("The database path cannot be empty")

        self.database_path = Path(raw_path)
        if raw_path != ":memory:":
            self.database_path.parent.mkdir(parents=True, exist_ok=True)

        self._connection: sqlite3.Connection | None = sqlite3.connect(
            raw_path,
            timeout=30.0,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA busy_timeout=30000")
            _ensure_schema(self._connection)
        except Exception:
            self._connection.close()
            self._connection = None
            raise

    def __enter__(self) -> StateStore:
        self._require_connection()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StateStoreError("StateStore is closed")
        return self._connection

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> UploadStatusRecord:
        return UploadStatusRecord(
            video_path=Path(row["video_path"]),
            platform=Platform(row["platform"]),
            status=UploadStatus(row["status"]),
            platform_video_id=row["platform_video_id"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            created_at=_deserialize_datetime(row["created_at"]),
            updated_at=_deserialize_datetime(row["updated_at"]),
            captions_uploaded=bool(row["captions_uploaded"]),
            playlist_added=bool(row["playlist_added"]),
        )

    def get_status(
        self,
        video_path: str | Path,
        platform: str | Platform,
    ) -> UploadStatusRecord | None:
        normalized_path = _normalize_video_path(video_path)
        normalized_platform = _coerce_platform(platform)
        row = self._require_connection().execute(
            """
            SELECT video_path, platform, status, platform_video_id, attempts,
                   last_error, created_at, updated_at, captions_uploaded, playlist_added
            FROM upload_status
            WHERE video_path = ? AND platform = ?
            """,
            (str(normalized_path), normalized_platform.value),
        ).fetchone()
        return None if row is None else self._record_from_row(row)

    def get_or_create_status(
        self,
        video_path: str | Path,
        platform: str | Platform,
    ) -> UploadStatusRecord:
        normalized_path = _normalize_video_path(video_path)
        normalized_platform = _coerce_platform(platform)
        now = _serialize_datetime(_utc_now())
        connection = self._require_connection()
        with connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO upload_status (
                    video_path, platform, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(normalized_path),
                    normalized_platform.value,
                    UploadStatus.PENDING.value,
                    now,
                    now,
                ),
            )
        record = self.get_status(normalized_path, normalized_platform)
        if record is None:  # pragma: no cover - SQLite integrity safeguard
            raise StateStoreError("Could not create the status record")
        return record

    def _set_status(
        self,
        video_path: str | Path,
        platform: str | Platform,
        status: UploadStatus,
        *,
        platform_video_id: str | None = None,
        last_error: str | None = None,
        increment_attempts: bool = False,
    ) -> UploadStatusRecord:
        current = self.get_or_create_status(video_path, platform)
        if current.status in {UploadStatus.SUCCESS, UploadStatus.SKIPPED}:
            if current.status is status:
                # An idempotent repeat may fill in the ID without changing attempts.
                if status is UploadStatus.SUCCESS and platform_video_id is not None:
                    connection = self._require_connection()
                    with connection:
                        connection.execute(
                            """
                            UPDATE upload_status
                            SET platform_video_id = ?, updated_at = ?
                            WHERE video_path = ? AND platform = ?
                            """,
                            (
                                platform_video_id,
                                _serialize_datetime(_utc_now()),
                                str(current.video_path),
                                current.platform.value,
                            ),
                        )
                    return self.get_status(current.video_path, current.platform)  # type: ignore[return-value]
                return current
            raise StateStoreError(
                f"Cannot change terminal status {current.status.value} "
                f"to {status.value}"
            )

        connection = self._require_connection()
        with connection:
            connection.execute(
                """
                UPDATE upload_status
                SET status = ?,
                    platform_video_id = COALESCE(?, platform_video_id),
                    attempts = attempts + ?,
                    last_error = ?,
                    updated_at = ?
                WHERE video_path = ? AND platform = ?
                """,
                (
                    status.value,
                    platform_video_id,
                    1 if increment_attempts else 0,
                    last_error,
                    _serialize_datetime(_utc_now()),
                    str(current.video_path),
                    current.platform.value,
                ),
            )
        record = self.get_status(current.video_path, current.platform)
        if record is None:  # pragma: no cover
            raise StateStoreError("The record disappeared during the update")
        return record

    def mark_in_progress(
        self, video_path: str | Path, platform: str | Platform
    ) -> UploadStatusRecord:
        return self._set_status(video_path, platform, UploadStatus.IN_PROGRESS)

    def mark_success(
        self,
        video_path: str | Path,
        platform: str | Platform,
        platform_video_id: str | None = None,
    ) -> UploadStatusRecord:
        return self._set_status(
            video_path,
            platform,
            UploadStatus.SUCCESS,
            platform_video_id=platform_video_id,
            increment_attempts=True,
        )

    def mark_skipped(
        self,
        video_path: str | Path,
        platform: str | Platform,
        reason: str,
    ) -> UploadStatusRecord:
        if not isinstance(reason, str) or not reason.strip():
            raise StateStoreError("The skip reason cannot be empty")
        return self._set_status(
            video_path,
            platform,
            UploadStatus.SKIPPED,
            last_error=reason.strip(),
        )

    def mark_failed(
        self,
        video_path: str | Path,
        platform: str | Platform,
        error_message: str,
    ) -> UploadStatusRecord:
        if not isinstance(error_message, str) or not error_message.strip():
            raise StateStoreError("The error message cannot be empty")
        return self._set_status(
            video_path,
            platform,
            UploadStatus.FAILED,
            last_error=error_message.strip(),
            increment_attempts=True,
        )

    def _mark_flag(
        self,
        video_path: str | Path,
        platform: str | Platform,
        column: str,
    ) -> UploadStatusRecord:
        current = self.get_or_create_status(video_path, platform)
        if current.status is not UploadStatus.SUCCESS:
            raise StateStoreError(
                f"Cannot set {column} before a successful video upload"
            )
        connection = self._require_connection()
        with connection:
            connection.execute(
                f"""
                UPDATE upload_status
                SET {column} = 1, updated_at = ?
                WHERE video_path = ? AND platform = ?
                """,
                (
                    _serialize_datetime(_utc_now()),
                    str(current.video_path),
                    current.platform.value,
                ),
            )
        record = self.get_status(current.video_path, current.platform)
        if record is None:  # pragma: no cover
            raise StateStoreError("The record disappeared while updating the flag")
        return record

    def mark_captions_uploaded(
        self, video_path: str | Path, platform: str | Platform
    ) -> UploadStatusRecord:
        return self._mark_flag(video_path, platform, "captions_uploaded")

    def mark_playlist_added(
        self, video_path: str | Path, platform: str | Platform
    ) -> UploadStatusRecord:
        normalized_platform = _coerce_platform(platform)
        if normalized_platform is not Platform.YOUTUBE:
            raise StateStoreError("playlist_added is available only for YouTube")
        return self._mark_flag(video_path, normalized_platform, "playlist_added")

    def is_fully_processed(
        self,
        video_path: str | Path,
        required_platforms: Iterable[str | Platform],
    ) -> bool:
        platforms = {_coerce_platform(item) for item in required_platforms}
        if not platforms:
            return False
        return all(
            (record := self.get_status(video_path, platform)) is not None
            and record.status in {UploadStatus.SUCCESS, UploadStatus.SKIPPED}
            for platform in platforms
        )

    def get_quota_usage(self, bucket: str, period: str) -> int:
        """Return locally reserved units for a quota period."""
        if not bucket.strip() or not period.strip():
            raise StateStoreError("Quota bucket and period cannot be empty")
        row = self._require_connection().execute(
            """
            SELECT units FROM api_quota_usage
            WHERE platform = ? AND quota_period = ?
            """,
            (bucket.strip().lower(), period.strip()),
        ).fetchone()
        return 0 if row is None else int(row["units"])

    def try_reserve_quota(
        self,
        bucket: str,
        period: str,
        cost: int,
        limit: int,
    ) -> tuple[bool, int]:
        """Reserve quota atomically and return ``(success, usage_after_operation)``."""
        if isinstance(cost, bool) or not isinstance(cost, int) or cost <= 0:
            raise StateStoreError("cost must be a positive integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
        ):
            raise StateStoreError("limit must be a positive integer")
        normalized_bucket = bucket.strip().lower()
        normalized_period = period.strip()
        if not normalized_bucket or not normalized_period:
            raise StateStoreError("Quota bucket and period cannot be empty")

        connection = self._require_connection()
        now = _serialize_datetime(_utc_now())
        with connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO api_quota_usage (
                    platform, quota_period, units, updated_at
                ) VALUES (?, ?, 0, ?)
                """,
                (normalized_bucket, normalized_period, now),
            )
            cursor = connection.execute(
                """
                UPDATE api_quota_usage
                SET units = units + ?, updated_at = ?
                WHERE platform = ? AND quota_period = ?
                  AND units + ? <= ?
                """,
                (
                    cost,
                    now,
                    normalized_bucket,
                    normalized_period,
                    cost,
                    limit,
                ),
            )
        usage = self.get_quota_usage(normalized_bucket, normalized_period)
        return cursor.rowcount == 1, usage
