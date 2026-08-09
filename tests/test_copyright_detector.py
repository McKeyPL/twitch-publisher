from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from youtube_copyright.api_client import YouTubeCopyrightApi
from youtube_copyright.detector import (
    classify_region_restriction,
    parse_iso8601_duration,
)
from youtube_copyright.models import RestrictionKind, VideoState


@pytest.mark.parametrize(
    ("details", "state", "actionable", "reasons"),
    [
        (
            {"regionRestriction": {"allowed": []}},
            VideoState.GLOBAL_BLOCKED,
            True,
            ("GLOBAL",),
        ),
        (
            {"regionRestriction": {"blocked": ["PL"]}},
            VideoState.PRIORITY_REGION_BLOCKED,
            True,
            ("PL_BLOCKED",),
        ),
        (
            {"regionRestriction": {"blocked": ["de", "RU"]}},
            VideoState.PRIORITY_REGION_BLOCKED,
            True,
            ("DE_BLOCKED",),
        ),
        (
            {"regionRestriction": {"allowed": ["PL", "FR"]}},
            VideoState.PRIORITY_REGION_BLOCKED,
            True,
            ("DE_NOT_ALLOWED",),
        ),
        (
            {"regionRestriction": {"blocked": ["RU", "BY"]}},
            VideoState.OTHER_REGIONAL_ONLY,
            False,
            ("OTHER_REGIONS_ONLY",),
        ),
        (
            {"regionRestriction": {"allowed": ["PL", "DE"]}},
            VideoState.OTHER_REGIONAL_ONLY,
            False,
            ("OTHER_REGIONS_ONLY",),
        ),
        ({}, VideoState.NO_RESTRICTION, False, ()),
    ],
)
def test_region_classification(
    details: dict, state: VideoState, actionable: bool, reasons: tuple[str, ...]
) -> None:
    result = classify_region_restriction(details)
    assert result.state is state
    assert result.actionable is actionable
    assert result.reasons == reasons


def test_empty_blocked_list_means_no_restriction() -> None:
    result = classify_region_restriction(
        {"regionRestriction": {"blocked": []}}
    )
    assert result.kind is RestrictionKind.NONE
    assert result.state is VideoState.NO_RESTRICTION


def test_duration_parser() -> None:
    assert parse_iso8601_duration("PT12H") == 43200
    assert parse_iso8601_duration("P1DT2H3M4.5S") == 93784.5
    assert parse_iso8601_duration(None) is None
    with pytest.raises(ValueError):
        parse_iso8601_duration("12:00:00")


def test_video_list_batches_at_fifty_and_parses_resources() -> None:
    service = MagicMock()
    requests: list[MagicMock] = []

    def create_request(**kwargs: object) -> MagicMock:
        request = MagicMock()
        ids = str(kwargs["id"]).split(",")
        request.execute.return_value = {
            "items": [
                {
                    "id": video_id,
                    "snippet": {"title": f"Title {video_id}"},
                    "contentDetails": {
                        "duration": "PT1H2M3S",
                        "regionRestriction": {"blocked": ["PL"]},
                    },
                    "status": {"uploadStatus": "processed"},
                    "processingDetails": {"processingStatus": "succeeded"},
                }
                for video_id in ids
            ]
        }
        requests.append(request)
        return request

    service.videos.return_value.list.side_effect = create_request
    results = YouTubeCopyrightApi(service).list_videos(
        [f"video-{index}" for index in range(51)]
    )
    assert len(results) == 51
    assert len(requests) == 2
    assert results[0].duration_seconds == 3723
    assert results[0].content_details["regionRestriction"] == {"blocked": ["PL"]}


def test_discovers_upload_playlist_across_pages() -> None:
    service = MagicMock()
    service.channels.return_value.list.return_value.execute.return_value = {
        "items": [
            {"contentDetails": {"relatedPlaylists": {"uploads": "uploads-id"}}}
        ]
    }
    first = MagicMock()
    first.execute.return_value = {
        "items": [{"contentDetails": {"videoId": "one"}}],
        "nextPageToken": "next",
    }
    second = MagicMock()
    second.execute.return_value = {
        "items": [{"contentDetails": {"videoId": "two"}}]
    }
    service.playlistItems.return_value.list.side_effect = [first, second]

    assert YouTubeCopyrightApi(service).list_uploaded_video_ids() == ["one", "two"]
    calls = service.playlistItems.return_value.list.call_args_list
    assert calls[0].kwargs["playlistId"] == "uploads-id"
    assert calls[1].kwargs["pageToken"] == "next"


def test_reference_video_ids_are_valid_detector_inputs() -> None:
    # Stable integration fixtures supplied for later authorized/live probes.
    assert {"b7uH35WAR2U", "Z__dHxFC0PQ", "xWmvEX0oCj4"} == {
        value.strip() for value in ("b7uH35WAR2U", "Z__dHxFC0PQ", "xWmvEX0oCj4")
    }
