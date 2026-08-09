from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from config import load_config
from state import StateStore
from youtube_copyright.models import VideoState
from youtube_copyright.service import CopyrightGuardService
from youtube_copyright.state import CopyrightStateStore


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
        config = load_config(PROJECT_ROOT / "config.yaml", dotenv_path=tmp_path / "none")
    from dataclasses import replace

    return replace(
        config,
        paths=replace(config.paths, database=tmp_path / "state.sqlite3"),
    )


def _service_for(resources: list[dict]) -> MagicMock:
    service = MagicMock()
    request = MagicMock()
    request.execute.return_value = {"items": resources}
    service.videos.return_value.list.return_value = request
    return service


def _resource(video_id: str, restriction: dict | None) -> dict:
    details = {"duration": "PT2H"}
    if restriction is not None:
        details["regionRestriction"] = restriction
    return {
        "id": video_id,
        "snippet": {"title": video_id},
        "contentDetails": details,
        "status": {"uploadStatus": "processed"},
        "processingDetails": {"processingStatus": "succeeded"},
    }


def test_cycle_classifies_reference_scenarios_and_reserves_quota(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ids = ["b7uH35WAR2U", "Z__dHxFC0PQ", "xWmvEX0oCj4"]
    service = _service_for(
        [
            _resource(ids[0], {"allowed": []}),
            _resource(ids[1], {"blocked": ["PL", "RU"]}),
            _resource(ids[2], None),
        ]
    )
    with (
        StateStore(config.paths.database) as quota_store,
        CopyrightStateStore(config.paths.database) as copyright_store,
    ):
        result = CopyrightGuardService(
            config,
            copyright_store,
            quota_store,
            api_service=service,
        ).run_cycle(video_ids=ids, include_channel_uploads=False)

        assert result.actionable_video_ids == tuple(ids[:2])
        assert result.ignored_video_ids == (ids[2],)
        assert copyright_store.get_video(ids[0]).state is VideoState.GLOBAL_BLOCKED
        assert (
            copyright_store.get_video(ids[1]).state
            is VideoState.PRIORITY_REGION_BLOCKED
        )
        assert copyright_store.get_video(ids[2]).state is VideoState.NO_RESTRICTION
        used = quota_store._require_connection().execute(
            "SELECT SUM(units) FROM api_quota_usage WHERE platform = 'youtube_general'"
        ).fetchone()[0]
        assert used == 1


def test_missing_video_is_recorded_without_crashing_other_items(tmp_path: Path) -> None:
    config = _config(tmp_path)
    service = _service_for([_resource("present", None)])
    with (
        StateStore(config.paths.database) as quota_store,
        CopyrightStateStore(config.paths.database) as copyright_store,
    ):
        result = CopyrightGuardService(
            config,
            copyright_store,
            quota_store,
            api_service=service,
        ).run_cycle(
            video_ids=["present", "missing"], include_channel_uploads=False
        )
        assert result.missing_video_ids == ("missing",)
        assert copyright_store.get_video("missing").state is VideoState.FAILED
