"""YouTube Data API v3 uploader with OAuth2, resumable upload, and captions."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httplib2
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from config import RetryConfig, YouTubeConfig
from state import StateStore
from uploaders.base import BaseUploader, UploadResult


logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
RETRIABLE_HTTP_STATUS_CODES = {500, 502, 503, 504}
VIDEO_CHUNK_SIZE = 50 * 1024 * 1024
PACIFIC_TIME = ZoneInfo("America/Los_Angeles")


def _pacific_quota_window(now: datetime | None = None) -> tuple[str, datetime]:
    """Return the Pacific Time day key and the next PT midnight."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    pacific_now = current.astimezone(PACIFIC_TIME)
    next_date = pacific_now.date() + timedelta(days=1)
    next_reset = datetime.combine(next_date, datetime_time.min, tzinfo=PACIFIC_TIME)
    return pacific_now.date().isoformat(), next_reset


def _youtube_error_reason(error: HttpError) -> str | None:
    try:
        content = error.content.decode("utf-8") if isinstance(error.content, bytes) else error.content
        payload = json.loads(content)
        errors = payload.get("error", {}).get("errors", [])
        if errors:
            return errors[0].get("reason")
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return None


def _friendly_youtube_error(error: Exception) -> str:
    if not isinstance(error, HttpError):
        return str(error)

    reason = _youtube_error_reason(error)
    special_messages = {
        "quotaExceeded": "YouTube API quota has been exceeded (quotaExceeded)",
        "dailyLimitExceeded": "YouTube API daily quota has been exceeded",
        "videoTooLong": "YouTube rejected the video as too long (videoTooLong)",
        "uploadLimitExceeded": (
            "YouTube daily upload-count limit has been exceeded "
            "(uploadLimitExceeded), independently of API quota"
        ),
    }
    if reason in special_messages:
        return special_messages[reason]
    status = getattr(error.resp, "status", "?")
    return f"YouTube API HTTP {status}: {reason or error}"


def _is_retriable_error(error: Exception) -> bool:
    if isinstance(error, HttpError):
        return int(getattr(error.resp, "status", 0)) in RETRIABLE_HTTP_STATUS_CODES
    return isinstance(error, (OSError, TimeoutError, httplib2.HttpLib2Error))


class YouTubeUploader(BaseUploader):
    def __init__(
        self,
        config: YouTubeConfig,
        retry_config: RetryConfig,
        state_store: StateStore,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        super().__init__(retry_config, cancel_event)
        self.config = config
        self.state_store = state_store
        self._service: Any | None = None

    @property
    def platform_name(self) -> str:
        return "youtube"

    def _get_credentials(self) -> Credentials:
        credentials: Credentials | None = None
        token_path = self.config.token_file

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
                str(self.config.client_secrets_file),
                SCOPES,
            )
            logger.info("Opening a browser for YouTube OAuth2 authorization")
            credentials = flow.run_local_server(port=0, open_browser=True)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        return credentials

    def _get_service(self) -> Any:
        if self._service is None:
            self._service = build(
                "youtube",
                "v3",
                credentials=self._get_credentials(),
                cache_discovery=False,
            )
        return self._service

    def _prepare_srt(self, srt_path: Path | None) -> tuple[Path | None, str | None]:
        if srt_path is None:
            return None, None
        srt = Path(srt_path)
        if not srt.is_file():
            raise FileNotFoundError(f"SRT file was not found: {srt}")
        size = srt.stat().st_size
        if size == 0:
            logger.info("Empty SRT will not be uploaded to YouTube: %s", srt)
            return None, None
        maximum = int(self.config.srt_max_size_mb * 1024 * 1024)
        if size > maximum:
            warning = (
                f"SRT {srt} is {size} bytes and exceeds the "
                f"{self.config.srt_max_size_mb:g} MB limit; captions were skipped"
            )
            logger.warning(warning)
            return None, warning
        return srt, None

    def _reserve_quota(self, include_captions: bool) -> tuple[bool, str | None]:
        period, next_reset = _pacific_quota_window()
        reservations = [
            (
                "youtube_videos_insert",
                self.config.upload_quota_units,
                self.config.daily_upload_limit,
                "videos.insert",
            )
        ]
        if include_captions:
            reservations.append(
                (
                    "youtube_general",
                    self.config.captions_quota_units,
                    self.config.daily_quota_units,
                    "captions.insert",
                )
            )

        # Preflighting all buckets prevents partial reservation in normal,
        # single-process use. StateStore's atomic condition also prevents races.
        for bucket, cost, limit, operation in reservations:
            current = self.state_store.get_quota_usage(bucket, period)
            if current + cost > limit:
                message = (
                    f"Local YouTube quota limit for {operation} would be exceeded: "
                    f"used {current}/{limit}, operation requires {cost}. "
                    f"Next reset at Pacific Time midnight: {next_reset.isoformat()} "
                    f"({next_reset.astimezone(timezone.utc).isoformat()} UTC). "
                    "Check the authoritative granular limit in Google Cloud Console."
                )
                logger.error(message)
                return False, message

        for bucket, cost, limit, operation in reservations:
            reserved, usage = self.state_store.try_reserve_quota(
                bucket, period, cost, limit
            )
            if not reserved:  # possible only with a concurrent second process
                message = (
                    f"Could not reserve quota for {operation} because another process "
                    "used the limit concurrently. Retry after the PT midnight reset."
                )
                logger.error(message)
                return False, message
            logger.info(
                "YouTube: reserved %d for %s (%d/%d, PT period %s)",
                cost,
                operation,
                usage,
                limit,
                period,
            )
        return True, None

    def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str],
        srt_path: Path | None = None,
    ) -> UploadResult:
        video = Path(video_path)
        if not video.is_file():
            return UploadResult(False, error_message=f"Video file was not found: {video}")
        if not title.strip():
            return UploadResult(False, error_message="YouTube title cannot be empty")

        try:
            usable_srt, captions_warning = self._prepare_srt(srt_path)
        except OSError as exc:
            return UploadResult(False, error_message=str(exc))

        quota_ok, quota_error = self._reserve_quota(usable_srt is not None)
        if not quota_ok:
            return UploadResult(False, error_message=quota_error)

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": self.config.category_id,
            },
            "status": {"privacyStatus": self.config.privacy_status},
        }

        try:
            media = MediaFileUpload(
                str(video),
                chunksize=VIDEO_CHUNK_SIZE,
                resumable=True,
            )
            request = self._get_service().videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )
            response: dict[str, Any] | None = None
            while response is None:
                self._raise_if_cancelled()
                _, response = self._with_retry(
                    request.next_chunk,
                    operation_name=f"uploading chunk of {video.name}",
                    should_retry=_is_retriable_error,
                )
        except Exception as exc:
            message = _friendly_youtube_error(exc)
            logger.exception("YouTube upload failed: %s", message)
            return UploadResult(False, error_message=message)

        video_id = response.get("id") if response else None
        if not video_id:
            return UploadResult(False, error_message="YouTube returned no video ID after upload")
        video_url = f"https://youtube.com/watch?v={video_id}"

        captions_uploaded = False
        captions_error = captions_warning
        if usable_srt is not None:
            try:
                captions_media = MediaFileUpload(
                    str(usable_srt),
                    mimetype="application/octet-stream",
                    resumable=False,
                )
                captions_request = self._get_service().captions().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "videoId": video_id,
                            "language": self.config.captions_language,
                            "name": self.config.captions_name,
                            "isDraft": False,
                        }
                    },
                    media_body=captions_media,
                )
                self._with_retry(
                    captions_request.execute,
                    operation_name=f"adding captions to {video_id}",
                    should_retry=_is_retriable_error,
                )
                captions_uploaded = True
            except Exception as exc:
                captions_error = f"Video uploaded, but captions were not added: {_friendly_youtube_error(exc)}"
                logger.exception(captions_error)

        logger.info("YouTube upload completed: %s", video_url)
        return UploadResult(
            success=True,
            platform_video_id=video_id,
            platform_url=video_url,
            error_message=captions_error,
            captions_uploaded=captions_uploaded,
        )

    def _create_playlist(self, playlist_title: str) -> str:
        request = self._get_service().playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": playlist_title,
                    "description": f"Automatic stream archive: {playlist_title}",
                },
                "status": {"privacyStatus": self.config.privacy_status},
            },
        )
        response = self._with_retry(
            request.execute,
            operation_name=f"creating playlist {playlist_title}",
            should_retry=_is_retriable_error,
        )
        playlist_id = response.get("id")
        if not playlist_id:
            raise RuntimeError("YouTube returned no ID for the new playlist")

        env_name = "YT_PLAYLIST_" + re.sub(r"[^A-Z0-9]+", "_", playlist_title.upper()).strip("_")
        logger.warning(
            "Created playlist %s for %s. Save %s=%s in .env; otherwise the "
            "next process may create another playlist.",
            playlist_id,
            playlist_title,
            env_name,
            playlist_id,
        )
        return playlist_id

    def add_to_playlist(
        self,
        platform_video_id: str,
        playlist_identifier: str,
        *,
        playlist_title: str | None = None,
    ) -> bool:
        if not platform_video_id.strip():
            logger.error("Cannot add to a playlist without a video ID")
            return False

        playlist_id = playlist_identifier.strip()
        try:
            if playlist_id:
                lookup = self._get_service().playlists().list(
                    part="id",
                    id=playlist_id,
                    maxResults=1,
                )
                lookup_response = self._with_retry(
                    lookup.execute,
                    operation_name=f"checking playlist {playlist_id}",
                    should_retry=_is_retriable_error,
                )
                if not lookup_response.get("items"):
                    logger.warning("Playlist %s does not exist", playlist_id)
                    playlist_id = ""

            if not playlist_id:
                if not playlist_title or not playlist_title.strip():
                    logger.error(
                        "playlist_id and playlist_title are missing; cannot create a playlist"
                    )
                    return False
                playlist_id = self._create_playlist(playlist_title.strip())

            insert = self._get_service().playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": platform_video_id.strip(),
                        },
                    }
                },
            )
            self._with_retry(
                insert.execute,
                operation_name=f"adding {platform_video_id} to playlist {playlist_id}",
                should_retry=_is_retriable_error,
            )
            logger.info("Added video %s to playlist %s", platform_video_id, playlist_id)
            return True
        except Exception as exc:
            logger.exception("Could not add video to playlist: %s", _friendly_youtube_error(exc))
            return False
