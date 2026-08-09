"""Extract copyright claims from the changing YouTube Studio DOM."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .models import ClaimType, CopyrightClaim, RemediationAction


_TIME_RANGE = re.compile(
    r"(?P<start>(?:\d{1,2}:)?\d{1,2}:\d{2})\s*(?:-|–|—|to)\s*"
    r"(?P<end>(?:\d{1,2}:)?\d{1,2}:\d{2})",
    re.IGNORECASE,
)

_ACTION_ALIASES: dict[RemediationAction, tuple[str, ...]] = {
    RemediationAction.ERASE_SONG: (
        "erase song",
        "remove song",
        "usuń utwór",
        "wymaż utwór",
    ),
    RemediationAction.MUTE_ALL: (
        "mute all sound",
        "mute all audio",
        "wycisz cały dźwięk",
        "wycisz cały dzwiek",
    ),
    RemediationAction.TRIM: (
        "trim out segment",
        "trim segment",
        "wytnij fragment",
        "przytnij fragment",
    ),
}


@dataclass(frozen=True, slots=True)
class ParsedStudioClaim:
    claim: CopyrightClaim
    dom_index: int
    raw_text: str


class StudioClaimParser:
    def extract(self, page: Any, video_id: str) -> list[ParsedStudioClaim]:
        rows = page.evaluate(_CLAIM_EXTRACTION_SCRIPT)
        if not isinstance(rows, list):
            raise RuntimeError("Studio claim extraction returned an invalid value")
        return self.parse_rows(video_id, rows)

    def parse_rows(
        self, video_id: str, rows: Iterable[dict[str, Any]]
    ) -> list[ParsedStudioClaim]:
        results: list[ParsedStudioClaim] = []
        for index, row in enumerate(rows):
            text = _normalize_text(str(row.get("text", "")))
            if not text:
                continue
            actions_text = _normalize_text(str(row.get("actions", "")))
            combined = f"{text} {actions_text}".lower()
            claim_type = _claim_type(combined)
            start, end = _extract_range(text)
            actions = tuple(
                action
                for action, aliases in _ACTION_ALIASES.items()
                if any(alias in combined for alias in aliases)
            )
            content_title = _first_meaningful_line(text)
            # Do not include transient status/action labels in the identity. They
            # change after an edit and would otherwise reset erase->mute fallback.
            fingerprint = hashlib.sha256(
                f"{video_id}\0{claim_type.value}\0{content_title}\0{start}\0{end}".encode(
                    "utf-8"
                )
            ).hexdigest()
            claim = CopyrightClaim(
                fingerprint=fingerprint,
                video_id=video_id,
                claim_type=claim_type,
                content_title=content_title,
                start_seconds=start,
                end_seconds=end,
                available_actions=actions,
                actionable=claim_type is not ClaimType.STRIKE,
            )
            action_index = row.get("actionIndex", index)
            if not isinstance(action_index, int) or action_index < 0:
                action_index = index
            results.append(ParsedStudioClaim(claim, action_index, text))
        return results


def _normalize_text(value: str) -> str:
    return "\n".join(
        line.strip() for line in value.replace("\r", "\n").split("\n") if line.strip()
    )


def _first_meaningful_line(text: str) -> str | None:
    ignored = {"content used", "treść wykorzystana", "tresc wykorzystana"}
    for line in text.splitlines():
        if line.lower() not in ignored and not _TIME_RANGE.fullmatch(line):
            return line[:500]
    return None


def _claim_type(text: str) -> ClaimType:
    if any(value in text for value in ("copyright strike", "takedown", "ostrzeżenie")):
        return ClaimType.STRIKE
    audio = any(
        value in text
        for value in ("song", "music", "audio", "sound recording", "utwór", "muzyka", "dźwięk")
    )
    visual = any(
        value in text
        for value in ("visual", "audiovisual", "video segment", "obraz", "fragment filmu")
    )
    if audio and visual:
        return ClaimType.AUDIOVISUAL
    if visual:
        return ClaimType.VISUAL
    if audio:
        return ClaimType.AUDIO
    return ClaimType.UNKNOWN


def _extract_range(text: str) -> tuple[float | None, float | None]:
    match = _TIME_RANGE.search(text)
    if match is None:
        return None, None
    return _timestamp_seconds(match.group("start")), _timestamp_seconds(match.group("end"))


def _timestamp_seconds(value: str) -> float:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return float(parts[0] * 60 + parts[1])
    return float(parts[0] * 3600 + parts[1] * 60 + parts[2])


_CLAIM_EXTRACTION_SCRIPT = r"""
() => {
  const visible = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.height > 0;
  };
  const selectors = [
    'ytcr-video-content-list-row',
    'ytcp-video-copyright-claim-row',
    'ytcp-video-copyright-claim-details',
    'ytcp-copyright-claim-row',
    '[data-testid*="claim"]',
    '[class*="claim-row"]'
  ];
  let elements = [];
  for (const selector of selectors) {
    elements = Array.from(document.querySelectorAll(selector)).filter(visible);
    if (elements.length) break;
  }
  if (!elements.length) {
    const buttons = Array.from(document.querySelectorAll('button')).filter((button) =>
      /take action|select action|actions|podejmij działanie|wybierz działanie|działania/i.test(
        button.innerText || button.getAttribute('aria-label') || ''
      )
    );
    elements = buttons.map((button) =>
      button.closest(
        'ytcr-video-content-list-row, ytcp-video-copyright-claim-row, tr, [role="row"], .row'
      ) || button.parentElement
    ).filter(Boolean);
  }
  const allActionButtons = Array.from(document.querySelectorAll('button')).filter((button) =>
    visible(button) &&
    /take action|select action|actions|podejmij działanie|wybierz działanie|działania/i.test(
      button.innerText || button.getAttribute('aria-label') || ''
    )
  );
  return elements.map((element, index) => ({
    text: (element.innerText || '').trim(),
    actions: Array.from(element.querySelectorAll('button, [role="menuitem"], [role="option"]'))
      .map((item) => (item.innerText || item.getAttribute('aria-label') || '').trim())
      .filter(Boolean)
      .join('\n'),
    actionIndex: (() => {
      const button = Array.from(element.querySelectorAll('button')).find((candidate) =>
        /take action|select action|actions|podejmij działanie|wybierz działanie|działania/i.test(
          candidate.innerText || candidate.getAttribute('aria-label') || ''
        )
      );
      const actionIndex = button ? allActionButtons.indexOf(button) : index;
      return actionIndex >= 0 ? actionIndex : index;
    })()
  }));
}
"""
