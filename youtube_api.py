"""Shared, process-safe YouTube OAuth and Data API client."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import YouTubeConfig


logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

_PROCESS_LOCK = threading.Lock()


class TokenLockTimeout(TimeoutError):
    """The OAuth token lock could not be acquired within its deadline."""


class _TokenFileLock:
    def __init__(self, path: Path, timeout_seconds: float = 30.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._stream: Any | None = None

    def __enter__(self) -> "_TokenFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        self._stream.seek(0, os.SEEK_END)
        if self._stream.tell() == 0:
            self._stream.write(b"0")
            self._stream.flush()

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._try_lock()
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._stream.close()
                    self._stream = None
                    raise TokenLockTimeout(f"Timed out waiting for OAuth token lock {self.path}")
                time.sleep(0.1)

    def _try_lock(self) -> None:
        assert self._stream is not None
        if os.name == "nt":
            import msvcrt

            self._stream.seek(0)
            msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None


class YouTubeApiClient:
    """Lazily authorize and construct one YouTube Data API service."""

    def __init__(self, config: YouTubeConfig) -> None:
        self.config = config
        self._service: Any | None = None

    def get_credentials(self) -> Credentials:
        token_path = self.config.token_file
        lock_path = token_path.with_suffix(token_path.suffix + ".lock")
        with _PROCESS_LOCK, _TokenFileLock(lock_path):
            credentials: Credentials | None = None
            if token_path.is_file():
                try:
                    credentials = Credentials.from_authorized_user_file(token_path, SCOPES)
                except (OSError, ValueError) as exc:
                    logger.warning("Cannot use OAuth token %s: %s", token_path, exc)

            if credentials and credentials.expired and credentials.refresh_token:
                try:
                    credentials.refresh(Request())
                except RefreshError as exc:
                    logger.warning("Automatic token refresh failed: %s", exc)
                    credentials = None

            has_scopes = bool(credentials and credentials.has_scopes(SCOPES))
            if not credentials or not credentials.valid or not has_scopes:
                if self.config.client_secrets_file is None:
                    raise RuntimeError("client_secrets_file is missing for YouTube OAuth")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.config.client_secrets_file), SCOPES
                )
                logger.info("Opening a browser for YouTube OAuth2 authorization")
                credentials = flow.run_local_server(port=0, open_browser=True)

            self._write_token_atomically(token_path, credentials.to_json())
            return credentials

    @staticmethod
    def _write_token_atomically(token_path: Path, payload: str) -> None:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = token_path.with_name(
            f".{token_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, token_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not remove temporary OAuth token %s", temporary)

    def get_service(self) -> Any:
        if self._service is None:
            self._service = build(
                "youtube",
                "v3",
                credentials=self.get_credentials(),
                cache_discovery=False,
            )
        return self._service
