"""Pure YouTube region-restriction classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .models import RestrictionKind, VideoState


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


@dataclass(frozen=True, slots=True)
class RestrictionDecision:
    kind: RestrictionKind
    state: VideoState
    actionable: bool
    allowed_regions: tuple[str, ...] | None
    blocked_regions: tuple[str, ...] | None
    reasons: tuple[str, ...]


def parse_iso8601_duration(value: str | None) -> float | None:
    if not value:
        return None
    match = _DURATION_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"Unsupported ISO 8601 duration: {value!r}")
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def classify_region_restriction(
    content_details: Mapping[str, Any],
    *,
    protected_regions: tuple[str, ...] = ("PL", "DE"),
    global_blocks: bool = True,
    ignore_other_regional_blocks: bool = True,
) -> RestrictionDecision:
    protected = tuple(dict.fromkeys(region.strip().upper() for region in protected_regions))
    restriction = content_details.get("regionRestriction")
    if not isinstance(restriction, Mapping):
        return RestrictionDecision(
            kind=RestrictionKind.NONE,
            state=VideoState.NO_RESTRICTION,
            actionable=False,
            allowed_regions=None,
            blocked_regions=None,
            reasons=(),
        )

    if "allowed" in restriction:
        raw_allowed = restriction.get("allowed")
        allowed = _region_tuple(raw_allowed, "allowed")
        if not allowed:
            return RestrictionDecision(
                kind=RestrictionKind.GLOBAL,
                state=VideoState.GLOBAL_BLOCKED,
                actionable=global_blocks,
                allowed_regions=allowed,
                blocked_regions=None,
                reasons=("GLOBAL",),
            )
        reasons = tuple(
            f"{region}_NOT_ALLOWED" for region in protected if region not in allowed
        )
        if reasons:
            return RestrictionDecision(
                kind=RestrictionKind.PROTECTED_REGION,
                state=VideoState.PRIORITY_REGION_BLOCKED,
                actionable=True,
                allowed_regions=allowed,
                blocked_regions=None,
                reasons=reasons,
            )
        return RestrictionDecision(
            kind=RestrictionKind.OTHER_REGIONAL,
            state=VideoState.OTHER_REGIONAL_ONLY,
            actionable=not ignore_other_regional_blocks,
            allowed_regions=allowed,
            blocked_regions=None,
            reasons=("OTHER_REGIONS_ONLY",),
        )

    if "blocked" in restriction:
        blocked = _region_tuple(restriction.get("blocked"), "blocked")
        reasons = tuple(f"{region}_BLOCKED" for region in protected if region in blocked)
        if reasons:
            return RestrictionDecision(
                kind=RestrictionKind.PROTECTED_REGION,
                state=VideoState.PRIORITY_REGION_BLOCKED,
                actionable=True,
                allowed_regions=None,
                blocked_regions=blocked,
                reasons=reasons,
            )
        if not blocked:
            return RestrictionDecision(
                kind=RestrictionKind.NONE,
                state=VideoState.NO_RESTRICTION,
                actionable=False,
                allowed_regions=None,
                blocked_regions=blocked,
                reasons=(),
            )
        return RestrictionDecision(
            kind=RestrictionKind.OTHER_REGIONAL,
            state=VideoState.OTHER_REGIONAL_ONLY,
            actionable=not ignore_other_regional_blocks,
            allowed_regions=None,
            blocked_regions=blocked,
            reasons=("OTHER_REGIONS_ONLY",),
        )

    return RestrictionDecision(
        kind=RestrictionKind.UNKNOWN,
        state=VideoState.DISCOVERED,
        actionable=False,
        allowed_regions=None,
        blocked_regions=None,
        reasons=("MALFORMED_REGION_RESTRICTION",),
    )


def _region_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"regionRestriction.{field} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"regionRestriction.{field} contains a non-string value")
        region = item.strip().upper()
        if region and region not in result:
            result.append(region)
    return tuple(result)
