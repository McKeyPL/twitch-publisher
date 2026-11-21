"""Bound application logs and protect free space on the recording volume."""

import logging
import re
import shutil
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

MIB = 1024**2
GIB = 1024**3
DIAGNOSTIC_MAX_BYTES = 512 * MIB
MIN_FREE_BYTES = 10 * GIB
WARN_FREE_BYTES = 20 * GIB


class BoundedLogHandler(RotatingFileHandler):
    def __init__(self, path: Path) -> None:
        super().__init__(path, maxBytes=25 * MIB, backupCount=5, encoding="utf-8", delay=True)
        self._last_error = None

    def handleError(self, record: logging.LogRecord) -> None:
        now = time.monotonic()
        if self._last_error is None or now - self._last_error >= 300:
            self._last_error = now
            try:
                print(f"File logging failed for {self.baseFilename}: {sys.exc_info()[1]}", file=sys.stderr)
            except OSError:
                pass


class DiskSpaceMonitor:
    def __init__(self) -> None:
        self._levels: dict[Path, str] = {}

    def allows_large_writes(self, path: Path) -> bool:
        try:
            free = shutil.disk_usage(path).free
        except OSError:
            # Missing paths are handled by the caller; leave normal errors visible.
            return True
        level = "critical" if free < MIN_FREE_BYTES else "low" if free < WARN_FREE_BYTES else "normal"
        previous = self._levels.get(path, "normal")
        self._levels[path] = level
        if level != previous:
            logger = logging.getLogger(__name__)
            if level == "normal":
                logger.info("Disk space recovered for %s (%.1f GiB free)", path, free / GIB)
            else:
                logger.warning("Disk space %s for %s (%.1f GiB free)", level, path, free / GIB)
        return level != "critical"


_BROWSER_ARTIFACT = re.compile(r"(?:cda|rumble)_\d{8}_\d{6}(?:_\d{6})?_[\w.-]+\.(?:png|zip)")


def prune_browser_artifacts(directory: Path) -> None:
    if not directory.is_dir():
        return
    artifacts = []
    for path in directory.iterdir():
        if not _BROWSER_ARTIFACT.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            if path.is_file():
                info = path.stat()
                artifacts.append((info.st_mtime, path, info.st_size))
        except OSError:
            logging.getLogger(__name__).warning("Could not inspect %s", path, exc_info=True)
    cutoff = time.time() - 14 * 86400
    total = sum(size for _, _, size in artifacts)
    for modified, path, size in sorted(artifacts):
        if modified >= cutoff and total <= DIAGNOSTIC_MAX_BYTES:
            break
        try:
            path.unlink()
        except OSError:
            logging.getLogger(__name__).warning("Could not prune %s", path, exc_info=True)
            continue
        total -= size
