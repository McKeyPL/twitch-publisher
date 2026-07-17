from __future__ import annotations

from pathlib import Path

from duration_check import FileSizeStabilityTracker, ReadinessStatus
from watcher import iter_candidate_recordings, scan_cycle


def make_recording(
    directory: Path,
    stem: str,
    *,
    channel: str,
    ended: str | None = "2026-07-12T20:36:32",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / f"{stem}.mkv"
    video.write_bytes(b"video")
    ended_value = ended or ""
    video.with_name(f"{stem}_meta.txt").write_text(
        "\n".join(
            [
                f"Channel : {channel}",
                "Title : Testowy stream",
                "Game : Test: Game",
                "Started : 2026-07-12T17:24:22",
                f"Ended : {ended_value}",
                "Quality : best",
            ]
        ),
        encoding="utf-8",
    )
    return video


def test_two_cycles_transition_from_pending_to_ready(tmp_path: Path) -> None:
    video = make_recording(tmp_path / "mrozopl", "stream", channel="mrozopl")
    tracker = FileSizeStabilityTracker(60)

    first = scan_cycle(tmp_path, tracker, "_uploaded", now=100.0)
    second = scan_cycle(tmp_path, tracker, "_uploaded", now=160.0)

    assert [result.status for result in first] == [ReadinessStatus.SIZE_CHECK_PENDING]
    assert [result.status for result in second] == [ReadinessStatus.READY]
    assert second[0].metadata is not None
    assert second[0].metadata.source_path == video.with_name("stream_meta.txt")


def test_missing_meta_does_not_block_other_files(tmp_path: Path) -> None:
    channel = tmp_path / "ctsg"
    channel.mkdir()
    (channel / "missing.mkv").write_bytes(b"video")
    make_recording(channel, "complete", channel="ctsg")
    tracker = FileSizeStabilityTracker(1)

    results = scan_cycle(tmp_path, tracker, "_uploaded", now=10)

    assert {result.status for result in results} == {
        ReadinessStatus.META_MISSING,
        ReadinessStatus.SIZE_CHECK_PENDING,
    }


def test_ended_missing_is_reported(tmp_path: Path) -> None:
    make_recording(tmp_path / "perrydotto", "live", channel="perrydotto", ended=None)
    results = scan_cycle(tmp_path, FileSizeStabilityTracker(1), "_uploaded", now=0)
    assert results[0].status is ReadinessStatus.ENDED_MISSING


def test_uploaded_directories_are_skipped_at_every_depth(tmp_path: Path) -> None:
    wanted = make_recording(tmp_path / "buvanybu" / "nested", "wanted", channel="buvanybu")
    make_recording(tmp_path / "buvanybu" / "_uploaded", "old", channel="buvanybu")
    make_recording(
        tmp_path / "buvanybu" / "archive" / "_uploaded" / "deep",
        "old_deep",
        channel="buvanybu",
    )
    assert list(iter_candidate_recordings(tmp_path, "_uploaded")) == [wanted]


def test_video_directly_in_root_does_not_require_folder_channel(tmp_path: Path) -> None:
    make_recording(tmp_path, "root_stream", channel="any_channel")
    results = scan_cycle(tmp_path, FileSizeStabilityTracker(1), "_uploaded", now=0)
    assert results[0].status is ReadinessStatus.SIZE_CHECK_PENDING
