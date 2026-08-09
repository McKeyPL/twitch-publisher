from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from config import YouTubeConfig
from srt_splitter import parse_srt_text
from youtube_copyright.captions import (
    CopyrightCaptionManager,
    retime_srt_after_trims,
)


SRT = """1
00:00:05,000 --> 00:00:25,000
spans cut

2
00:00:30,000 --> 00:00:40,000
after cut
"""


def _config(tmp_path: Path) -> YouTubeConfig:
    return YouTubeConfig(
        enabled=True,
        client_secrets_file=tmp_path / "credentials.json",
        token_file=tmp_path / "token.json",
        privacy_status="unlisted",
        max_duration_hours=12,
        max_file_size_gb=256,
        title_limit=100,
        category_id="20",
        captions_language="pl",
        captions_name="Twitch Chat",
        daily_upload_limit=100,
        daily_quota_units=10000,
        upload_quota_units=1,
        captions_quota_units=400,
        srt_max_size_mb=100,
        playlists={},
    )


def test_retime_srt_splits_overlapping_cue_and_shifts_later_cues() -> None:
    adjusted = retime_srt_after_trims(SRT, ((10, 20),))
    cues = parse_srt_text(adjusted)
    assert [(cue.start_ms, cue.end_ms, cue.lines[0]) for cue in cues] == [
        (5000, 10000, "spans cut"),
        (10000, 15000, "spans cut"),
        (20000, 30000, "after cut"),
    ]


def test_backup_and_update_owned_caption_track(tmp_path: Path) -> None:
    service = MagicMock()
    service.captions.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": "asr",
                "snippet": {
                    "videoId": "video",
                    "language": "pl",
                    "name": "",
                    "trackKind": "ASR",
                    "status": "serving",
                },
            },
            {
                "id": "chat-track",
                "snippet": {
                    "videoId": "video",
                    "language": "pl",
                    "name": "Twitch Chat",
                    "trackKind": "standard",
                    "status": "serving",
                },
            },
        ]
    }
    service.captions.return_value.download.return_value.execute.return_value = SRT.encode()
    service.captions.return_value.update.return_value.execute.return_value = {
        "id": "chat-track",
        "snippet": {"status": "serving"},
    }
    quota: list[int] = []
    manager = CopyrightCaptionManager(
        service, _config(tmp_path), tmp_path / "backups", quota.append
    )
    backup = manager.backup_owned_track("video", 7)
    assert backup.track is not None
    assert backup.track.id == "chat-track"
    assert backup.original_path is not None and backup.original_path.is_file()

    with patch("youtube_copyright.captions.MediaFileUpload") as media:
        updated = manager.update_after_trim(backup, ((10, 20),))
    assert updated.status == "SERVING"
    assert updated.adjusted_path is not None and updated.adjusted_path.is_file()
    assert quota == [50, 1, 450]
    media.assert_called_once()
    kwargs = service.captions.return_value.update.call_args.kwargs
    assert kwargs["body"]["id"] == "chat-track"
    assert kwargs["body"]["snippet"]["language"] == "pl"


def test_no_owned_caption_track_is_nonfatal(tmp_path: Path) -> None:
    service = MagicMock()
    service.captions.return_value.list.return_value.execute.return_value = {"items": []}
    manager = CopyrightCaptionManager(
        service, _config(tmp_path), tmp_path / "backups", lambda cost: None
    )
    backup = manager.backup_owned_track("video", 1)
    assert backup.status == "NOT_PRESENT"
    assert backup.track is None
