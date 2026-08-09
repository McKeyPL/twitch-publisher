"""Deterministic and testable remediation policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from config import YouTubeCopyrightConfig

from .models import (
    ActionState,
    ClaimType,
    CopyrightAction,
    CopyrightClaim,
    RemediationAction,
)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: RemediationAction | None
    reason: str
    manual_required: bool = False
    wait_for_processing: bool = False


def choose_action(
    claim: CopyrightClaim,
    previous_actions: Iterable[CopyrightAction],
    config: YouTubeCopyrightConfig,
) -> PolicyDecision:
    actions = list(previous_actions)
    if claim.claim_type is ClaimType.STRIKE:
        return PolicyDecision(None, "Strikes and takedowns require legal review", True)
    if any(
        action.state in {ActionState.SUBMITTED, ActionState.PROCESSING}
        for action in actions
    ):
        return PolicyDecision(None, "A previous Studio edit is still processing", wait_for_processing=True)
    if any(action.state is ActionState.UNCERTAIN for action in actions):
        return PolicyDecision(
            None,
            "A previous edit was interrupted after its result became uncertain",
            manual_required=True,
        )

    available = set(claim.available_actions)

    def supported(action: RemediationAction) -> bool:
        # An empty list means that the menu has not been opened yet. The executor
        # still verifies the concrete option before it can click anything.
        return not available or action in available

    attempted = {
        action.action
        for action in actions
        if action.state in {ActionState.SUBMITTED, ActionState.PROCESSING, ActionState.SUCCEEDED}
    }

    if claim.claim_type is ClaimType.AUDIO:
        preferred = _configured_action(config.preferred_audio_action)
        fallback = _configured_action(config.audio_fallback_action)
        if preferred not in attempted and supported(preferred):
            return PolicyDecision(preferred, "Use the preferred audio remediation")
        if fallback not in attempted and supported(fallback):
            return PolicyDecision(fallback, "Preferred audio remediation did not resolve the restriction")
        if RemediationAction.TRIM not in attempted and supported(RemediationAction.TRIM):
            return PolicyDecision(RemediationAction.TRIM, "Audio removal options are exhausted; trim the claim")
        return PolicyDecision(None, "No untried audio remediation remains", True)

    if claim.claim_type in {ClaimType.VISUAL, ClaimType.AUDIOVISUAL}:
        if RemediationAction.TRIM not in attempted and supported(RemediationAction.TRIM):
            return PolicyDecision(RemediationAction.TRIM, "Visual claims require trimming")
        return PolicyDecision(None, "Studio does not offer an untried trim operation", True)

    return PolicyDecision(None, "The claim type cannot be classified safely", True)


def validate_trim_ranges(
    ranges: Iterable[tuple[float, float]],
    *,
    duration_seconds: float,
    max_trim_fraction: float,
    min_remaining_seconds: float,
) -> tuple[tuple[float, float], ...]:
    if duration_seconds <= 0:
        raise ValueError("Video duration must be greater than zero")
    normalized: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if start < 0 or end <= start or end > duration_seconds + 0.5:
            raise ValueError(f"Invalid trim range: {start:g}-{end:g}")
        bounded_end = min(end, duration_seconds)
        if normalized and start <= normalized[-1][1]:
            normalized[-1] = (normalized[-1][0], max(normalized[-1][1], bounded_end))
        else:
            normalized.append((start, bounded_end))
    if not normalized:
        raise ValueError("At least one unambiguous trim range is required")
    removed = sum(end - start for start, end in normalized)
    if removed / duration_seconds > max_trim_fraction:
        raise ValueError("Trim would remove more than the configured fraction of the video")
    if duration_seconds - removed < min_remaining_seconds:
        raise ValueError("Trim would leave less than the configured minimum duration")
    return tuple(normalized)


def _configured_action(value: str) -> RemediationAction:
    mapping = {
        "erase_song": RemediationAction.ERASE_SONG,
        "mute_all": RemediationAction.MUTE_ALL,
        "trim": RemediationAction.TRIM,
    }
    return mapping[value]
