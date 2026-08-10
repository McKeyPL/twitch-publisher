from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from config import load_config
from youtube_copyright.models import (
    ActionState,
    ClaimType,
    CopyrightAction,
    CopyrightClaim,
    RemediationAction,
)
from youtube_copyright.policy import choose_action, validate_trim_ranges
from youtube_copyright.studio_parser import StudioClaimParser


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path):
    with patch.dict(
        "os.environ",
        {
            "YOUTUBE_CLIENT_SECRETS_FILE": "auth/credentials.json",
            "RECORDINGS_ROOT": str(tmp_path / "recordings"),
        },
        clear=True,
    ):
        return load_config(
            PROJECT_ROOT / "config.yaml", dotenv_path=tmp_path / "missing.env"
        ).youtube_copyright


def _claim(claim_type: ClaimType = ClaimType.AUDIO) -> CopyrightClaim:
    return CopyrightClaim("fingerprint", "video", claim_type, actionable=True)


def _action(action: RemediationAction, state: ActionState) -> CopyrightAction:
    return CopyrightAction(None, "run", "video", action, state)


def test_audio_policy_uses_erase_then_mute(tmp_path: Path) -> None:
    config = _config(tmp_path)
    decision = choose_action(_claim(), [], config)
    assert decision.action is RemediationAction.ERASE_SONG

    decision = choose_action(
        _claim(), [_action(RemediationAction.ERASE_SONG, ActionState.SUCCEEDED)], config
    )
    assert decision.action is RemediationAction.MUTE_ALL


def test_policy_waits_for_submitted_edit(tmp_path: Path) -> None:
    decision = choose_action(
        _claim(),
        [_action(RemediationAction.ERASE_SONG, ActionState.SUBMITTED)],
        _config(tmp_path),
    )
    assert decision.action is None
    assert decision.wait_for_processing


def test_policy_never_retries_an_uncertain_interrupted_action(tmp_path: Path) -> None:
    decision = choose_action(
        _claim(),
        [_action(RemediationAction.ERASE_SONG, ActionState.UNCERTAIN)],
        _config(tmp_path),
    )
    assert decision.action is None
    assert decision.manual_required


def test_visual_claim_uses_trim_and_strike_is_manual(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert choose_action(_claim(ClaimType.VISUAL), [], config).action is RemediationAction.TRIM
    strike = choose_action(_claim(ClaimType.STRIKE), [], config)
    assert strike.action is None
    assert strike.manual_required


def test_trim_ranges_are_merged_and_guarded() -> None:
    assert validate_trim_ranges(
        [(10, 20), (19, 30), (50, 60)],
        duration_seconds=100,
        max_trim_fraction=0.9,
        min_remaining_seconds=10,
    ) == ((10, 30), (50, 60))
    with pytest.raises(ValueError, match="fraction"):
        validate_trim_ranges(
            [(0, 95)],
            duration_seconds=100,
            max_trim_fraction=0.9,
            min_remaining_seconds=1,
        )
    with pytest.raises(ValueError, match="minimum"):
        validate_trim_ranges(
            [(0, 50)],
            duration_seconds=100,
            max_trim_fraction=0.9,
            min_remaining_seconds=60,
        )


def test_parser_extracts_english_audio_visual_and_strike_claims() -> None:
    parsed = StudioClaimParser().parse_rows(
        "video",
        [
            {
                "text": "Test Song\nSound recording\nContent found during 01:10 - 02:20",
                "actions": "Erase song\nMute all sound in claimed segment",
            },
            {
                "text": "Claimed video segment\nVisual\n1:02:03 – 1:03:04",
                "actions": "Trim out segment",
            },
            {"text": "Copyright strike\nTakedown", "actions": "Appeal"},
        ],
    )
    assert [item.claim.claim_type for item in parsed] == [
        ClaimType.AUDIO,
        ClaimType.VISUAL,
        ClaimType.STRIKE,
    ]
    assert parsed[0].claim.start_seconds == 70
    assert parsed[0].claim.end_seconds == 140
    assert parsed[0].claim.available_actions == (
        RemediationAction.ERASE_SONG,
        RemediationAction.MUTE_ALL,
    )
    assert parsed[1].claim.start_seconds == 3723
    assert parsed[1].claim.available_actions == (RemediationAction.TRIM,)
    assert len({item.claim.fingerprint for item in parsed}) == 3


def test_claim_fingerprint_ignores_transient_status_text() -> None:
    parser = StudioClaimParser()
    first = parser.parse_rows(
        "video",
        [{"text": "Song title\nAudio\n00:10 - 00:20", "actions": "Erase song"}],
    )[0]
    later = parser.parse_rows(
        "video",
        [
            {
                "text": "Song title\nAudio\n00:10 - 00:20\nProcessing your edits",
                "actions": "Mute all sound",
            }
        ],
    )[0]
    assert first.claim.fingerprint == later.claim.fingerprint
