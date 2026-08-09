"""Read-only YouTube inventory and restriction API operations."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Callable

from .detector import parse_iso8601_duration


VIDEO_BATCH_SIZE = 50


@dataclass(frozen=True, slots=True)
class ApiVideo:
    video_id: str
    title: str | None
    duration_seconds: float | None
    content_details: dict[str, Any]
    upload_status: str | None
    rejection_reason: str | None
    processing_status: str | None


class YouTubeCopyrightApi:
    def __init__(self, service: Any, quota_callback: Callable[[int], None] | None = None) -> None:
        self.service = service
        self.quota_callback = quota_callback

    def _execute(self, request: Any, *, quota_cost: int = 1) -> dict[str, Any]:
        if self.quota_callback is not None:
            self.quota_callback(quota_cost)
        return request.execute()

    def list_uploaded_video_ids(self) -> list[str]:
        request = (
            self.service.channels()
            .list(part="contentDetails", mine=True, maxResults=1)
        )
        response = self._execute(request)
        channels = response.get("items", [])
        if len(channels) != 1:
            raise RuntimeError(
                f"Expected exactly one authorized YouTube channel, got {len(channels)}"
            )
        uploads_id = (
            channels[0]
            .get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads")
        )
        if not uploads_id:
            raise RuntimeError("The authorized channel has no uploads playlist")

        video_ids: list[str] = []
        page_token: str | None = None
        while True:
            request = self.service.playlistItems().list(
                part="contentDetails",
                playlistId=uploads_id,
                maxResults=50,
                pageToken=page_token,
            )
            page = self._execute(request)
            for item in page.get("items", []):
                video_id = item.get("contentDetails", {}).get("videoId")
                if video_id and video_id not in video_ids:
                    video_ids.append(video_id)
            page_token = page.get("nextPageToken")
            if not page_token:
                return video_ids

    def list_videos(self, video_ids: Iterable[str]) -> list[ApiVideo]:
        normalized = list(dict.fromkeys(value.strip() for value in video_ids if value.strip()))
        results: list[ApiVideo] = []
        for batch in _batches(normalized, VIDEO_BATCH_SIZE):
            request = (
                self.service.videos()
                .list(
                    part="contentDetails,processingDetails,snippet,status",
                    id=",".join(batch),
                    maxResults=len(batch),
                    fields=(
                        "items(id,snippet/title,contentDetails/duration,"
                        "contentDetails/regionRestriction,status/uploadStatus,"
                        "status/rejectionReason,processingDetails/processingStatus)"
                    ),
                )
            )
            response = self._execute(request)
            for item in response.get("items", []):
                content_details = dict(item.get("contentDetails", {}))
                status = item.get("status", {})
                processing = item.get("processingDetails", {})
                results.append(
                    ApiVideo(
                        video_id=item["id"],
                        title=item.get("snippet", {}).get("title"),
                        duration_seconds=parse_iso8601_duration(
                            content_details.get("duration")
                        ),
                        content_details=content_details,
                        upload_status=status.get("uploadStatus"),
                        rejection_reason=status.get("rejectionReason"),
                        processing_status=processing.get("processingStatus"),
                    )
                )
        return results


def _batches(values: list[str], size: int) -> Iterator[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]
