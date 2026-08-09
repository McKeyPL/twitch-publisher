"""Back up and retime owned caption tracks after Studio trims."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from googleapiclient.http import MediaFileUpload

from config import YouTubeConfig
from srt_splitter import SRTCue, parse_srt_text, render_srt


@dataclass(frozen=True, slots=True)
class CaptionTrack:
    id: str
    video_id: str
    language: str
    name: str
    track_kind: str
    status: str


@dataclass(frozen=True, slots=True)
class CaptionBackup:
    video_id: str
    action_id: int
    track: CaptionTrack | None
    original_path: Path | None
    adjusted_path: Path | None = None
    status: str = "PENDING"
    error_message: str | None = None


class CopyrightCaptionManager:
    def __init__(
        self,
        service: Any,
        config: YouTubeConfig,
        backup_root: Path,
        quota_callback: Callable[[int], None],
    ) -> None:
        self.service = service
        self.config = config
        self.backup_root = backup_root
        self.quota_callback = quota_callback

    def list_owned_tracks(self, video_id: str) -> list[CaptionTrack]:
        self.quota_callback(50)
        response = (
            self.service.captions()
            .list(part="id,snippet", videoId=video_id)
            .execute()
        )
        tracks: list[CaptionTrack] = []
        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            kind = str(snippet.get("trackKind", "unknown"))
            if kind.upper() == "ASR":
                continue
            tracks.append(
                CaptionTrack(
                    id=item["id"],
                    video_id=video_id,
                    language=str(snippet.get("language", "")),
                    name=str(snippet.get("name", "")),
                    track_kind=kind,
                    status=str(snippet.get("status", "")),
                )
            )
        return tracks

    def backup_owned_track(self, video_id: str, action_id: int) -> CaptionBackup:
        tracks = self.list_owned_tracks(video_id)
        preferred = [
            track
            for track in tracks
            if track.language == self.config.captions_language
            and track.name == self.config.captions_name
        ]
        if not preferred:
            preferred = [
                track for track in tracks if track.language == self.config.captions_language
            ]
        if not preferred:
            return CaptionBackup(video_id, action_id, None, None, status="NOT_PRESENT")
        if len(preferred) > 1:
            raise RuntimeError(
                f"More than one owned caption track matches video {video_id}"
            )
        track = preferred[0]
        self.quota_callback(1)
        payload = self.service.captions().download(id=track.id, tfmt="srt").execute()
        if isinstance(payload, str):
            text = payload
        elif isinstance(payload, bytes):
            text = payload.decode("utf-8-sig")
        else:
            raise RuntimeError("captions.download returned neither bytes nor text")
        parse_srt_text(text)
        directory = self.backup_root / video_id / str(action_id)
        directory.mkdir(parents=True, exist_ok=True)
        original = directory / "original.srt"
        original.write_text(text, encoding="utf-8", newline="\n")
        (directory / "track.json").write_text(
            json.dumps(
                {
                    "id": track.id,
                    "video_id": track.video_id,
                    "language": track.language,
                    "name": track.name,
                    "track_kind": track.track_kind,
                    "status": track.status,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return CaptionBackup(video_id, action_id, track, original)

    def get_track_status(self, video_id: str, track_id: str) -> str | None:
        for track in self.list_owned_tracks(video_id):
            if track.id == track_id:
                return track.status.upper()
        return None

    def update_after_trim(
        self,
        backup: CaptionBackup,
        trim_ranges: tuple[tuple[float, float], ...],
    ) -> CaptionBackup:
        if backup.track is None or backup.original_path is None:
            return CaptionBackup(
                backup.video_id,
                backup.action_id,
                backup.track,
                backup.original_path,
                status="NOT_PRESENT",
            )
        text = backup.original_path.read_text(encoding="utf-8-sig")
        adjusted_text = retime_srt_after_trims(text, trim_ranges)
        adjusted = backup.original_path.with_name("adjusted.srt")
        adjusted.write_text(adjusted_text, encoding="utf-8", newline="\n")
        self.quota_callback(450)
        request = self.service.captions().update(
            part="id,snippet",
            body={
                "id": backup.track.id,
                "snippet": {
                    "videoId": backup.video_id,
                    "language": backup.track.language,
                    "name": backup.track.name,
                    "isDraft": False,
                },
            },
            media_body=MediaFileUpload(
                str(adjusted), mimetype="application/octet-stream", resumable=False
            ),
        )
        response = request.execute()
        status = str(response.get("snippet", {}).get("status", "syncing"))
        return CaptionBackup(
            backup.video_id,
            backup.action_id,
            backup.track,
            backup.original_path,
            adjusted,
            status=status.upper(),
        )


def retime_srt_after_trims(
    text: str, trim_ranges: tuple[tuple[float, float], ...]
) -> str:
    ranges_ms = tuple(
        (round(start * 1000), round(end * 1000)) for start, end in trim_ranges
    )
    cues = parse_srt_text(text)
    output: list[SRTCue] = []
    for cue in cues:
        pieces = [(cue.start_ms, cue.end_ms)]
        for trim_start, trim_end in ranges_ms:
            next_pieces: list[tuple[int, int]] = []
            for start, end in pieces:
                if end <= trim_start or start >= trim_end:
                    next_pieces.append((start, end))
                    continue
                if start < trim_start:
                    next_pieces.append((start, trim_start))
                if end > trim_end:
                    next_pieces.append((trim_end, end))
            pieces = next_pieces
        for start, end in pieces:
            shifted_start = start - _removed_before(start, ranges_ms)
            shifted_end = end - _removed_before(end, ranges_ms)
            if shifted_end <= shifted_start:
                continue
            output.append(
                SRTCue(
                    start_ms=shifted_start,
                    end_ms=shifted_end,
                    lines=cue.lines,
                    settings=cue.settings,
                )
            )
    output.sort(key=lambda cue: (cue.start_ms, cue.end_ms))
    return render_srt(output)


def _removed_before(moment: int, ranges: tuple[tuple[int, int], ...]) -> int:
    return sum(max(0, min(moment, end) - start) for start, end in ranges if moment > start)
