"""Typed domain models for copyright detection and remediation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RestrictionKind(str, Enum):
    NONE = "NONE"
    GLOBAL = "GLOBAL"
    PROTECTED_REGION = "PROTECTED_REGION"
    OTHER_REGIONAL = "OTHER_REGIONAL"
    UNKNOWN = "UNKNOWN"


class VideoState(str, Enum):
    DISCOVERED = "DISCOVERED"
    NO_RESTRICTION = "NO_RESTRICTION"
    OTHER_REGIONAL_ONLY = "OTHER_REGIONAL_ONLY"
    GLOBAL_BLOCKED = "GLOBAL_BLOCKED"
    PRIORITY_REGION_BLOCKED = "PRIORITY_REGION_BLOCKED"
    ACTION_PENDING = "ACTION_PENDING"
    EDIT_SUBMITTED = "EDIT_SUBMITTED"
    PROCESSING = "PROCESSING"
    CAPTIONS_PENDING = "CAPTIONS_PENDING"
    RESOLVED = "RESOLVED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTOMATION_UNAVAILABLE = "AUTOMATION_UNAVAILABLE"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    FAILED = "FAILED"


class ClaimType(str, Enum):
    AUDIO = "AUDIO"
    VISUAL = "VISUAL"
    AUDIOVISUAL = "AUDIOVISUAL"
    STRIKE = "STRIKE"
    UNKNOWN = "UNKNOWN"


class RemediationAction(str, Enum):
    ERASE_SONG = "ERASE_SONG"
    MUTE_ALL = "MUTE_ALL"
    TRIM = "TRIM"


class ActionState(str, Enum):
    PLANNED = "PLANNED"
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    UNCERTAIN = "UNCERTAIN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class CopyrightVideo:
    video_id: str
    state: VideoState = VideoState.DISCOVERED
    restriction_kind: RestrictionKind = RestrictionKind.UNKNOWN
    source_video_path: str | None = None
    title: str | None = None
    duration_seconds: float | None = None
    allowed_regions: tuple[str, ...] | None = None
    blocked_regions: tuple[str, ...] | None = None
    restriction_reasons: tuple[str, ...] = ()
    processing: bool = False
    last_error: str | None = None
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CopyrightClaim:
    fingerprint: str
    video_id: str
    claim_type: ClaimType
    content_title: str | None = None
    claimant: str | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    available_actions: tuple[RemediationAction, ...] = ()
    actionable: bool = False
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CopyrightAction:
    id: int | None
    run_id: str
    video_id: str
    action: RemediationAction
    state: ActionState
    claim_fingerprint: str | None = None
    attempt: int = 1
    error_message: str | None = None
    trace_path: str | None = None
    before_screenshot: str | None = None
    confirmation_screenshot: str | None = None
    after_screenshot: str | None = None
    trim_ranges: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    created_at: datetime | None = None
    submitted_at: datetime | None = None
    completed_at: datetime | None = None
