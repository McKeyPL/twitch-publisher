"""Safe, correlated diagnostics for YouTube Studio automation."""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from config import CopyrightDiagnosticsConfig


logger = logging.getLogger(__name__)
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_component(value: str) -> str:
    result = _SAFE_COMPONENT.sub("_", value.strip()).strip("._")
    return result[:100] or "unknown"


def safe_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class DiagnosticRun:
    def __init__(
        self,
        config: CopyrightDiagnosticsConfig,
        run_id: str,
        video_id: str | None = None,
    ) -> None:
        self.config = config
        self.run_id = safe_component(run_id)
        self.video_id = safe_component(video_id) if video_id else None
        self.directory = config.directory / self.run_id
        if self.video_id:
            self.directory /= self.video_id
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, name: str, suffix: str) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        return self.directory / f"{timestamp}_{safe_component(name)}{suffix}"

    def screenshot(self, page: Any, stage: str, *, full_page: bool = True) -> Path:
        destination = self.path(stage, ".png")
        page.screenshot(path=str(destination), full_page=full_page)
        logger.info("Saved copyright screenshot: %s", destination)
        return destination

    def write_json(self, name: str, payload: Any) -> Path:
        destination = self.path(name, ".json")
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        temporary.replace(destination)
        return destination


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, Path)):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def prune_diagnostics(config: CopyrightDiagnosticsConfig) -> list[Path]:
    root = config.directory
    if not root.is_dir():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.retention_days)
    removed: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        modified = datetime.fromtimestamp(child.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            shutil.rmtree(child)
            removed.append(child)
    return removed
