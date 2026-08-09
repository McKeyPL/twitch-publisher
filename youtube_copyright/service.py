"""Copyright guard cycle orchestration, independent from publisher main.py."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from config import Config
from state import StateStore
from youtube_api import YouTubeApiClient

from .api_client import YouTubeCopyrightApi
from .browser_session import StudioAuthRequired, StudioBrowserManager
from .detector import classify_region_restriction
from .models import (
    ActionState,
    CopyrightAction,
    CopyrightVideo,
    RemediationAction,
    VideoState,
)
from .policy import choose_action, validate_trim_ranges
from .state import CopyrightStateStore
from .studio_executor import StudioCopyrightExecutor


logger = logging.getLogger(__name__)
PACIFIC_TIME = ZoneInfo("America/Los_Angeles")


class CopyrightQuotaExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CycleResult:
    run_id: str
    videos_checked: int
    actionable_video_ids: tuple[str, ...]
    ignored_video_ids: tuple[str, ...]
    missing_video_ids: tuple[str, ...]
    actions_submitted: int = 0


class CopyrightGuardService:
    def __init__(
        self,
        config: Config,
        copyright_store: CopyrightStateStore,
        quota_store: StateStore,
        *,
        api_service: Any | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.copyright_store = copyright_store
        self.quota_store = quota_store
        self.stop_event = stop_event or threading.Event()
        service = api_service or YouTubeApiClient(config.platforms.youtube).get_service()
        self.api = YouTubeCopyrightApi(service, self._reserve_quota)

    def _reserve_quota(self, cost: int) -> None:
        period = datetime.now(timezone.utc).astimezone(PACIFIC_TIME).date().isoformat()
        reserved, used = self.quota_store.try_reserve_quota(
            "youtube_general",
            period,
            cost,
            self.config.platforms.youtube.daily_quota_units,
        )
        if not reserved:
            raise CopyrightQuotaExceeded(
                "YouTube API quota cannot be reserved for copyright monitoring "
                f"({used}+{cost}>{self.config.platforms.youtube.daily_quota_units})"
            )

    def run_cycle(
        self,
        *,
        video_ids: Iterable[str] | None = None,
        include_channel_uploads: bool = True,
    ) -> CycleResult:
        run_id = uuid.uuid4().hex
        mode = self.config.youtube_copyright.mode
        self.copyright_store.start_run(run_id, mode)
        checked = 0
        actionable: list[str] = []
        ignored: list[str] = []
        missing: list[str] = []
        try:
            candidates: dict[str, str | None] = self.copyright_store.publisher_video_ids()
            if video_ids is not None:
                candidates.update({value.strip(): None for value in video_ids if value.strip()})
            if include_channel_uploads and video_ids is None:
                candidates.update(
                    {video_id: candidates.get(video_id) for video_id in self.api.list_uploaded_video_ids()}
                )

            for video_id, source_path in candidates.items():
                if self.copyright_store.get_video(video_id) is None:
                    self.copyright_store.upsert_video(
                        CopyrightVideo(video_id=video_id, source_video_path=source_path)
                    )

            resources = self.api.list_videos(candidates)
            by_id = {resource.video_id: resource for resource in resources}
            now = datetime.now(timezone.utc)
            next_check = now + timedelta(hours=self.config.youtube_copyright.interval_hours)

            for video_id, source_path in candidates.items():
                if self.stop_event.is_set():
                    break
                resource = by_id.get(video_id)
                if resource is None:
                    missing.append(video_id)
                    self.copyright_store.upsert_video(
                        CopyrightVideo(
                            video_id=video_id,
                            source_video_path=source_path,
                            state=VideoState.FAILED,
                            last_error="The authorized videos.list response omitted this video",
                            last_checked_at=now,
                            next_check_at=next_check,
                        )
                    )
                    continue

                decision = classify_region_restriction(
                    resource.content_details,
                    protected_regions=self.config.youtube_copyright.protected_regions,
                    global_blocks=self.config.youtube_copyright.global_blocks,
                    ignore_other_regional_blocks=(
                        self.config.youtube_copyright.ignore_other_regional_blocks
                    ),
                )
                processing = resource.processing_status not in {None, "succeeded"}
                state = VideoState.PROCESSING if processing else decision.state
                latest_action = self.copyright_store.latest_action(video_id)
                if (
                    not decision.actionable
                    and latest_action is not None
                    and latest_action.state in {ActionState.SUBMITTED, ActionState.PROCESSING}
                ):
                    self.copyright_store.update_action(
                        latest_action.id, ActionState.SUCCEEDED
                    )
                    state = VideoState.RESOLVED
                if resource.rejection_reason in {"claim", "copyright"}:
                    state = VideoState.MANUAL_REQUIRED
                self.copyright_store.upsert_video(
                    CopyrightVideo(
                        video_id=video_id,
                        source_video_path=source_path,
                        title=resource.title,
                        duration_seconds=resource.duration_seconds,
                        state=state,
                        restriction_kind=decision.kind,
                        allowed_regions=decision.allowed_regions,
                        blocked_regions=decision.blocked_regions,
                        restriction_reasons=decision.reasons,
                        processing=processing,
                        last_checked_at=now,
                        next_check_at=next_check,
                    )
                )
                checked += 1
                if decision.actionable and not processing:
                    actionable.append(video_id)
                    logger.warning(
                        "YouTube video %s requires remediation: %s",
                        video_id,
                        ", ".join(decision.reasons),
                    )
                else:
                    ignored.append(video_id)
                    logger.info(
                        "YouTube video %s requires no action: %s",
                        video_id,
                        decision.state.value,
                    )

            submitted = 0
            if mode != "report":
                for video_id in actionable[: self.config.youtube_copyright.max_actions_per_cycle]:
                    if self.stop_event.is_set():
                        break
                    if self._remediate_video(video_id, run_id):
                        submitted += 1

            result = CycleResult(
                run_id=run_id,
                videos_checked=checked,
                actionable_video_ids=tuple(actionable),
                ignored_video_ids=tuple(ignored),
                missing_video_ids=tuple(missing),
                actions_submitted=submitted,
            )
            self.copyright_store.finish_run(
                run_id,
                status="SUCCESS",
                videos_checked=checked,
                actions_submitted=result.actions_submitted,
            )
            return result
        except Exception as exc:
            self.copyright_store.finish_run(
                run_id,
                status="FAILED",
                videos_checked=checked,
                error_message=str(exc),
            )
            raise

    def _remediate_video(self, video_id: str, run_id: str) -> bool:
        video = self.copyright_store.get_video(video_id)
        if video is None:
            return False
        manager = StudioBrowserManager(
            self.config.youtube_copyright.browser,
            self.config.youtube_copyright.diagnostics,
        )
        action_record: CopyrightAction | None = None
        next_check = datetime.now(timezone.utc) + timedelta(
            hours=self.config.youtube_copyright.interval_hours
        )
        try:
            with manager.open(run_id, video_id=video_id) as session:
                executor = StudioCopyrightExecutor(session.page, session.diagnostic)
                inspection = executor.inspect(video_id)
                if inspection.processing:
                    latest = self.copyright_store.latest_action(video_id)
                    if latest and latest.state is ActionState.SUBMITTED:
                        self.copyright_store.update_action(latest.id, ActionState.PROCESSING)
                    self.copyright_store.update_video_state(
                        video_id,
                        VideoState.PROCESSING,
                        processing=True,
                        next_check_at=next_check,
                    )
                    return False

                latest = self.copyright_store.latest_action(video_id)
                if latest and latest.state in {ActionState.SUBMITTED, ActionState.PROCESSING}:
                    self.copyright_store.update_action(
                        latest.id,
                        ActionState.SUCCEEDED,
                        error_message=(
                            "Studio processing ended, but the protected restriction remains"
                        ),
                    )

                claims = [item.claim for item in inspection.claims]
                self.copyright_store.replace_claims(video_id, claims)
                if not inspection.claims:
                    self.copyright_store.update_video_state(
                        video_id,
                        VideoState.AUTOMATION_UNAVAILABLE,
                        last_error="Studio exposed no parseable Content ID claims",
                        next_check_at=next_check,
                    )
                    return False

                parsed_claim = _select_claim(inspection.claims)
                history = self.copyright_store.actions_for_video(
                    video_id, claim_fingerprint=parsed_claim.claim.fingerprint
                )
                decision = choose_action(
                    parsed_claim.claim,
                    history,
                    self.config.youtube_copyright,
                )
                if decision.wait_for_processing:
                    self.copyright_store.update_video_state(
                        video_id,
                        VideoState.PROCESSING,
                        processing=True,
                        next_check_at=next_check,
                    )
                    return False
                if decision.action is None:
                    state = (
                        VideoState.MANUAL_REQUIRED
                        if decision.manual_required
                        else VideoState.AUTOMATION_UNAVAILABLE
                    )
                    self.copyright_store.update_video_state(
                        video_id,
                        state,
                        last_error=decision.reason,
                        next_check_at=next_check,
                    )
                    return False

                trim_ranges: tuple[tuple[float, float], ...] = ()
                if decision.action is RemediationAction.TRIM:
                    if (
                        video.duration_seconds is None
                        or parsed_claim.claim.start_seconds is None
                        or parsed_claim.claim.end_seconds is None
                    ):
                        raise ValueError(
                            "Trim requires video duration and an unambiguous claim range"
                        )
                    trim_ranges = validate_trim_ranges(
                        [
                            (
                                parsed_claim.claim.start_seconds,
                                parsed_claim.claim.end_seconds,
                            )
                        ],
                        duration_seconds=video.duration_seconds,
                        max_trim_fraction=self.config.youtube_copyright.max_trim_fraction,
                        min_remaining_seconds=(
                            self.config.youtube_copyright.min_remaining_seconds
                        ),
                    )

                action_record = self.copyright_store.add_action(
                    CopyrightAction(
                        id=None,
                        run_id=run_id,
                        video_id=video_id,
                        claim_fingerprint=parsed_claim.claim.fingerprint,
                        action=decision.action,
                        state=ActionState.PLANNED,
                        attempt=len(history) + 1,
                        trace_path=str(session.trace_path) if session.trace_path else None,
                        trim_ranges=trim_ranges,
                    )
                )
                result = executor.execute(
                    video_id,
                    parsed_claim,
                    decision.action,
                    dry_run=self.config.youtube_copyright.mode == "dry_run",
                    trace_path=session.trace_path,
                )
                if result.dry_run:
                    self.copyright_store.update_action(
                        action_record.id,
                        ActionState.CANCELLED,
                        error_message="Dry run: final confirmation was not clicked",
                        before_screenshot=str(result.before_screenshot),
                        confirmation_screenshot=str(result.confirmation_screenshot),
                    )
                    self.copyright_store.update_video_state(
                        video_id,
                        VideoState.ACTION_PENDING,
                        next_check_at=next_check,
                    )
                    return False
                self.copyright_store.update_action(
                    action_record.id,
                    ActionState.SUBMITTED,
                    trace_path=str(result.trace_path) if result.trace_path else None,
                    before_screenshot=str(result.before_screenshot),
                    confirmation_screenshot=str(result.confirmation_screenshot),
                    after_screenshot=str(result.after_screenshot),
                )
                self.copyright_store.update_video_state(
                    video_id,
                    VideoState.EDIT_SUBMITTED,
                    processing=True,
                    next_check_at=next_check,
                )
                return True
        except StudioAuthRequired as exc:
            self.copyright_store.update_video_state(
                video_id,
                VideoState.AUTH_REQUIRED,
                last_error=str(exc),
                next_check_at=next_check,
            )
            logger.error("YouTube Studio authentication required: %s", exc)
            return False
        except Exception as exc:
            if action_record is not None and action_record.id is not None:
                self.copyright_store.update_action(
                    action_record.id, ActionState.FAILED, error_message=str(exc)
                )
            self.copyright_store.update_video_state(
                video_id,
                VideoState.FAILED,
                last_error=str(exc),
                next_check_at=next_check,
            )
            logger.exception("Copyright remediation failed for %s", video_id)
            return False


def _select_claim(claims: Iterable[Any]) -> Any:
    claim_list = list(claims)
    priority_words = (
        "blocked worldwide",
        "blocked in poland",
        "blocked in germany",
        "zablokowany na całym świecie",
        "polska",
        "niemcy",
    )
    for parsed in claim_list:
        if any(word in parsed.raw_text.lower() for word in priority_words):
            return parsed
    return claim_list[0]
