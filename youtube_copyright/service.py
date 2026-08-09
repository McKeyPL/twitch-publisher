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
from .detector import classify_region_restriction
from .models import CopyrightVideo, VideoState
from .state import CopyrightStateStore


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

            result = CycleResult(
                run_id=run_id,
                videos_checked=checked,
                actionable_video_ids=tuple(actionable),
                ignored_video_ids=tuple(ignored),
                missing_video_ids=tuple(missing),
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
