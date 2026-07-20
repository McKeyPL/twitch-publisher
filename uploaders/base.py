"""Wspolny kontrakt uploaderow oraz retry z exponential backoff."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from config import RetryConfig


logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class UploadResult:
    success: bool
    platform_video_id: str | None = None
    platform_url: str | None = None
    error_message: str | None = None
    captions_uploaded: bool = False
    retry_allowed: bool = True


class BaseUploader(ABC):
    def __init__(self, retry_config: RetryConfig) -> None:
        self.retry_config = retry_config

    @property
    @abstractmethod
    def platform_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str],
        srt_path: Path | None = None,
    ) -> UploadResult:
        raise NotImplementedError

    @abstractmethod
    def add_to_playlist(
        self,
        platform_video_id: str,
        playlist_identifier: str,
        *,
        playlist_title: str | None = None,
    ) -> bool:
        raise NotImplementedError

    def _with_retry(
        self,
        operation: Callable[[], T],
        *,
        operation_name: str,
        should_retry: Callable[[Exception], bool] | None = None,
    ) -> T:
        """Uruchamia operacje z limitem prob i wykladniczym opoznieniem."""
        delay = self.retry_config.initial_backoff_seconds
        for attempt in range(1, self.retry_config.max_attempts + 1):
            try:
                logger.info(
                    "%s: %s, proba %d/%d",
                    self.platform_name,
                    operation_name,
                    attempt,
                    self.retry_config.max_attempts,
                )
                return operation()
            except Exception as exc:
                retry_allowed = should_retry(exc) if should_retry else True
                is_last = attempt >= self.retry_config.max_attempts
                logger.warning(
                    "%s: %s nie powiodlo sie w probie %d/%d: %s",
                    self.platform_name,
                    operation_name,
                    attempt,
                    self.retry_config.max_attempts,
                    exc,
                )
                if not retry_allowed or is_last:
                    raise
                sleep_seconds = min(delay, self.retry_config.max_backoff_seconds)
                logger.info(
                    "%s: ponowienie za %.1f s", self.platform_name, sleep_seconds
                )
                time.sleep(sleep_seconds)
                delay *= self.retry_config.multiplier

        raise RuntimeError("Nieosiagalny koniec petli retry")  # pragma: no cover
