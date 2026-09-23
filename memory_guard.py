"""Bound upload memory pressure using OS commit and physical-memory counters.

The size of a VOD is deliberately not part of the calculation: all supported
upload paths pass a file handle/path and must use bounded chunks.  The reserve
represents transient buffers used by the HTTP or browser process, while the
minimum headroom protects the rest of the Windows host (including Hyper-V).
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from config import MemoryConfig


logger = logging.getLogger(__name__)
GIB = 1024**3
MIB = 1024**2


class MemoryPressureError(RuntimeError):
    """The host has too little commit/physical headroom for a safe operation."""


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    commit_used_bytes: int | None
    commit_limit_bytes: int | None
    physical_available_bytes: int | None
    source: str

    @property
    def commit_available_bytes(self) -> int | None:
        if self.commit_used_bytes is None or self.commit_limit_bytes is None:
            return None
        return max(0, self.commit_limit_bytes - self.commit_used_bytes)


def _windows_memory_snapshot() -> MemorySnapshot:
    """Read the same system-wide commit counters exposed by Performance Monitor."""

    class PerformanceInformation(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("CommitTotal", ctypes.c_size_t),
            ("CommitLimit", ctypes.c_size_t),
            ("CommitPeak", ctypes.c_size_t),
            ("PhysicalTotal", ctypes.c_size_t),
            ("PhysicalAvailable", ctypes.c_size_t),
            ("SystemCache", ctypes.c_size_t),
            ("KernelTotal", ctypes.c_size_t),
            ("KernelPaged", ctypes.c_size_t),
            ("KernelNonpaged", ctypes.c_size_t),
            ("PageSize", ctypes.c_size_t),
            ("HandleCount", ctypes.c_uint32),
            ("ProcessCount", ctypes.c_uint32),
            ("ThreadCount", ctypes.c_uint32),
        ]

    information = PerformanceInformation()
    information.cb = ctypes.sizeof(information)
    get_performance_info = ctypes.WinDLL("psapi", use_last_error=True).GetPerformanceInfo
    get_performance_info.argtypes = [
        ctypes.POINTER(PerformanceInformation),
        ctypes.c_uint32,
    ]
    get_performance_info.restype = ctypes.c_int
    if not get_performance_info(ctypes.byref(information), information.cb):
        error = ctypes.get_last_error()
        raise OSError(error, "GetPerformanceInfo failed")
    page_size = int(information.PageSize)
    return MemorySnapshot(
        commit_used_bytes=int(information.CommitTotal) * page_size,
        commit_limit_bytes=int(information.CommitLimit) * page_size,
        physical_available_bytes=int(information.PhysicalAvailable) * page_size,
        source="Windows GetPerformanceInfo",
    )


def _linux_memory_snapshot() -> MemorySnapshot:
    values: dict[str, int] = {}
    with Path("/proc/meminfo").open("r", encoding="ascii") as stream:
        for line in stream:
            key, separator, remainder = line.partition(":")
            if not separator:
                continue
            fields = remainder.strip().split()
            if fields and fields[0].isdigit():
                values[key] = int(fields[0]) * 1024
    commit_limit = values.get("CommitLimit")
    committed = values.get("Committed_AS")
    return MemorySnapshot(
        commit_used_bytes=committed,
        commit_limit_bytes=commit_limit,
        physical_available_bytes=values.get("MemAvailable"),
        source="Linux /proc/meminfo",
    )


def system_memory_snapshot() -> MemorySnapshot:
    if os.name == "nt":
        return _windows_memory_snapshot()
    if Path("/proc/meminfo").is_file():
        return _linux_memory_snapshot()
    return MemorySnapshot(None, None, None, "unsupported operating system")


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value / GIB:.2f} GiB"


class MemoryGuard:
    """Rate-limited host-memory preflight used before and during long operations."""

    def __init__(
        self,
        config: MemoryConfig,
        *,
        snapshot_provider: Callable[[], MemorySnapshot] = system_memory_snapshot,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._snapshot_provider = snapshot_provider
        self._monotonic = monotonic
        self._last_checked_at: float | None = None
        self._last_snapshot: MemorySnapshot | None = None
        self._monitoring_warning_logged = False

    def _snapshot(self, *, force: bool) -> MemorySnapshot | None:
        if not self.config.enabled:
            return None
        now = self._monotonic()
        if (
            not force
            and self._last_snapshot is not None
            and self._last_checked_at is not None
            and now - self._last_checked_at < self.config.check_interval_seconds
        ):
            return self._last_snapshot
        try:
            snapshot = self._snapshot_provider()
        except Exception as exc:
            if not self._monitoring_warning_logged:
                logger.warning(
                    "Memory guard cannot read host counters and will fail open: %s",
                    exc,
                )
                self._monitoring_warning_logged = True
            return None
        self._last_snapshot = snapshot
        self._last_checked_at = now
        return snapshot

    def log_snapshot(self) -> MemorySnapshot | None:
        snapshot = self._snapshot(force=True)
        if snapshot is None:
            logger.info("Memory guard is disabled or host counters are unavailable")
            return None
        logger.info(
            "Memory guard: commit used=%s, limit=%s, headroom=%s, "
            "physical available=%s (%s)",
            format_bytes(snapshot.commit_used_bytes),
            format_bytes(snapshot.commit_limit_bytes),
            format_bytes(snapshot.commit_available_bytes),
            format_bytes(snapshot.physical_available_bytes),
            snapshot.source,
        )
        return snapshot

    def ensure_safe(
        self,
        operation: str,
        *,
        reserve_bytes: int = 0,
        force: bool = False,
    ) -> MemorySnapshot | None:
        """Raise before a new allocation when host headroom is below the policy."""

        if reserve_bytes < 0:
            raise ValueError("reserve_bytes must be non-negative")
        snapshot = self._snapshot(force=force)
        if snapshot is None:
            return None

        minimum_commit = int(self.config.minimum_commit_headroom_gb * GIB)
        required_commit = minimum_commit + reserve_bytes
        minimum_physical = int(self.config.minimum_physical_available_gb * GIB)
        problems: list[str] = []
        commit_available = snapshot.commit_available_bytes
        if commit_available is not None and commit_available < required_commit:
            problems.append(
                f"commit headroom {format_bytes(commit_available)} is below required "
                f"{format_bytes(required_commit)} (includes transient reserve "
                f"{format_bytes(reserve_bytes)})"
            )
        if (
            snapshot.physical_available_bytes is not None
            and snapshot.physical_available_bytes < minimum_physical
        ):
            problems.append(
                f"physical available {format_bytes(snapshot.physical_available_bytes)} "
                f"is below required {format_bytes(minimum_physical)}"
            )
        if problems:
            raise MemoryPressureError(
                f"Memory guard stopped {operation}: {'; '.join(problems)}. "
                "No VOD data was intentionally buffered; increase host/pagefile "
                "headroom or wait for memory pressure to fall."
            )
        return snapshot


def mebibytes(value: float) -> int:
    return int(value * MIB)
