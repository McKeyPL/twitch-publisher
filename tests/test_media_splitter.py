from __future__ import annotations

import csv
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from media_splitter import (
    MediaSplitError,
    MediaSplitter,
    SplitConstraints,
)


def make_source(tmp_path: Path, size: int = 1_000) -> Path:
    source = tmp_path / "stream.mkv"
    source.write_bytes(b"x" * size)
    return source


@pytest.mark.parametrize(
    "invalid",
    (
        "../work",
        r"..\work",
        r"C:\work",
        r"C:work",
        "/tmp/work",
        r"\\server\share",
        "work/subdirectory",
        r"work\subdirectory",
        ".",
        "..",
    ),
)
def test_work_directory_must_be_one_cross_platform_safe_name(
    invalid: str,
) -> None:
    with pytest.raises(ValueError, match="one safe directory name"):
        MediaSplitter(work_directory_name=invalid)


def emit_parts(
    work_directory: Path,
    rows: list[tuple[str, float, float, int]],
) -> None:
    work_directory.mkdir(parents=True, exist_ok=True)
    with (work_directory / "parts.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle)
        for name, start, end, size in rows:
            (work_directory / name).write_bytes(b"x" * size)
            writer.writerow((name, start, end))


def test_creates_lossless_plan_and_splits_srt_on_csv_boundaries(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    srt = tmp_path / "stream_chat.srt"
    srt.write_text(
        "1\n00:00:09,500 --> 00:00:10,500\ncrossing\n",
        encoding="utf-8",
    )
    splitter = MediaSplitter(
        disk_space_multiplier=1.01,
        duration_probe=lambda _path: 10.0,
    )

    def fake_ffmpeg(
        _source: Path,
        work_directory: Path,
        _segment_time: float,
    ) -> None:
        emit_parts(
            work_directory,
            [
                ("part_000.mkv", 0.0, 9.8, 450),
                ("part_001.mkv", 9.8, 20.0, 450),
            ],
        )

    with patch.object(splitter, "_run_ffmpeg", side_effect=fake_ffmpeg):
        plan = splitter.create_plan(
            source,
            "youtube",
            20,
            SplitConstraints(
                hard_max_duration_seconds=12,
                target_duration_seconds=10,
            ),
            srt_path=srt,
        )

    assert len(plan.parts) == 2
    assert plan.parts[0].end_seconds == 9.8
    assert plan.parts[1].srt_path is not None
    assert "00:00:00,000 --> 00:00:00,700" in plan.parts[
        1
    ].srt_path.read_text(encoding="utf-8")
    assert plan.manifest_path.is_file()


def test_reuses_verified_manifest_without_running_ffmpeg_again(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    splitter = MediaSplitter(
        disk_space_multiplier=1.01,
        duration_probe=lambda _path: 10.0,
    )
    constraints = SplitConstraints(hard_max_size_bytes=600, target_size_bytes=500)

    def fake_ffmpeg(
        _source: Path,
        work_directory: Path,
        _segment_time: float,
    ) -> None:
        emit_parts(
            work_directory,
            [
                ("part_000.mkv", 0.0, 10.0, 500),
                ("part_001.mkv", 10.0, 20.0, 500),
            ],
        )

    with patch.object(splitter, "_run_ffmpeg", side_effect=fake_ffmpeg) as run:
        first = splitter.create_plan(source, "rumble", 20, constraints)
        second = splitter.create_plan(source, "rumble", 20, constraints)

    assert first.reused is False
    assert second.reused is True
    assert run.call_count == 1


def test_changed_srt_invalidates_manifest_and_regenerates_captions(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    srt = tmp_path / "stream_chat.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nold\n",
        encoding="utf-8",
    )
    splitter = MediaSplitter(
        disk_space_multiplier=1.01,
        duration_probe=lambda _path: 10.0,
    )
    constraints = SplitConstraints(hard_max_duration_seconds=20)

    def fake_ffmpeg(
        _source: Path,
        work_directory: Path,
        _segment_time: float,
    ) -> None:
        emit_parts(work_directory, [("part_000.mkv", 0.0, 10.0, 500)])

    with patch.object(splitter, "_run_ffmpeg", side_effect=fake_ffmpeg) as run:
        first = splitter.create_plan(
            source,
            "youtube",
            10,
            constraints,
            srt_path=srt,
        )
        srt.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nnew text\n",
            encoding="utf-8",
        )
        second = splitter.create_plan(
            source,
            "youtube",
            10,
            constraints,
            srt_path=srt,
        )

    assert first.reused is False
    assert second.reused is False
    assert run.call_count == 2
    assert second.parts[0].srt_path is not None
    assert "new text" in second.parts[0].srt_path.read_text(encoding="utf-8")


def test_invalid_srt_does_not_block_media_parts(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    srt = tmp_path / "broken_chat.srt"
    srt.write_text("not a valid SRT", encoding="utf-8")
    splitter = MediaSplitter(
        disk_space_multiplier=1.01,
        duration_probe=lambda _path: 10.0,
    )

    def fake_ffmpeg(
        _source: Path,
        work_directory: Path,
        _segment_time: float,
    ) -> None:
        emit_parts(work_directory, [("part_000.mkv", 0.0, 10.0, 500)])

    with patch.object(splitter, "_run_ffmpeg", side_effect=fake_ffmpeg):
        plan = splitter.create_plan(
            source,
            "youtube",
            10,
            SplitConstraints(hard_max_duration_seconds=20),
            srt_path=srt,
        )

    assert len(plan.parts) == 1
    assert plan.parts[0].srt_path is None


def test_replans_with_shorter_segments_when_actual_part_exceeds_hard_limit(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    splitter = MediaSplitter(
        max_replans=2,
        disk_space_multiplier=1.01,
        duration_probe=lambda _path: 10.0,
    )
    constraints = SplitConstraints(hard_max_size_bytes=500, target_size_bytes=450)
    target_times: list[float] = []

    def fake_ffmpeg(
        _source: Path,
        work_directory: Path,
        segment_time: float,
    ) -> None:
        target_times.append(segment_time)
        if len(target_times) == 1:
            emit_parts(work_directory, [("part_000.mkv", 0.0, 10.0, 550)])
        else:
            emit_parts(
                work_directory,
                [
                    ("part_000.mkv", 0.0, 8.0, 440),
                    ("part_001.mkv", 8.0, 20.0, 440),
                ],
            )

    with patch.object(splitter, "_run_ffmpeg", side_effect=fake_ffmpeg):
        plan = splitter.create_plan(source, "rumble", 20, constraints)

    assert len(target_times) == 2
    assert target_times[1] < target_times[0]
    assert all(part.size_bytes <= 500 for part in plan.parts)


def test_fails_after_replan_budget_when_part_stays_oversized(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    splitter = MediaSplitter(
        max_replans=1,
        disk_space_multiplier=1.01,
        duration_probe=lambda _path: 20.0,
    )

    def fake_ffmpeg(
        _source: Path,
        work_directory: Path,
        _segment_time: float,
    ) -> None:
        emit_parts(work_directory, [("part_000.mkv", 0.0, 20.0, 700)])

    with (
        patch.object(splitter, "_run_ffmpeg", side_effect=fake_ffmpeg),
        pytest.raises(MediaSplitError, match="within hard limits"),
    ):
        splitter.create_plan(
            source,
            "rumble",
            20,
            SplitConstraints(hard_max_size_bytes=500),
        )


def test_cancelled_plan_never_starts_ffmpeg(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    cancel_event = threading.Event()
    cancel_event.set()
    splitter = MediaSplitter(
        cancel_event=cancel_event,
        disk_space_multiplier=1.01,
        duration_probe=lambda _path: 10.0,
    )

    with (
        patch.object(splitter, "_run_ffmpeg") as run,
        pytest.raises(KeyboardInterrupt),
    ):
        splitter.create_plan(
            source,
            "youtube",
            20,
            SplitConstraints(hard_max_duration_seconds=10),
        )

    run.assert_not_called()


def test_ffmpeg_command_uses_stream_copy_and_segment_csv(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    splitter = MediaSplitter()
    process = patch("media_splitter.subprocess.Popen").start()
    process.return_value.poll.return_value = 0
    process.return_value.returncode = 0
    process.return_value.stdout = []
    (work / "parts.csv").write_text("", encoding="utf-8")
    try:
        splitter._run_ffmpeg(source, work, 42.0)
    finally:
        patch.stopall()

    command = process.call_args.args[0]
    assert command[command.index("-c") + 1] == "copy"
    assert command[command.index("-segment_list_type") + 1] == "csv"
    mapped_streams = [
        command[index + 1]
        for index, argument in enumerate(command)
        if argument == "-map"
    ]
    assert mapped_streams == ["0:v?", "0:a?", "0:s?"]
    assert "0" not in mapped_streams
