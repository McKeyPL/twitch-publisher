from __future__ import annotations

from pathlib import Path

import pytest

from srt_splitter import (
    SRTError,
    SegmentWindow,
    cues_for_window,
    parse_srt_text,
    read_srt,
    render_srt,
    split_srt_file,
)


REAL_CHAT = """\
1
00:00:01,000 --> 00:00:03,000
user: Cześć! 📖

2
00:00:09,500 --> 00:00:10,500
user: wiadomość na granicy

3
00:00:12,000 --> 00:00:14,000
user: druga część ⚙️
druga linia
"""


def test_parse_and_render_preserves_emoji_and_multiline_text() -> None:
    cues = parse_srt_text(REAL_CHAT)

    assert len(cues) == 3
    assert cues[0].lines == ("user: Cześć! 📖",)
    assert cues[2].lines == ("user: druga część ⚙️", "druga linia")
    assert "📖" in render_srt(cues)
    assert "druga linia" in render_srt(cues)


def test_cue_crossing_boundary_is_clipped_into_both_parts() -> None:
    cues = parse_srt_text(REAL_CHAT)

    first = cues_for_window(cues, SegmentWindow(1, 0.0, 10.0))
    second = cues_for_window(cues, SegmentWindow(2, 10.0, 20.0))

    assert first[-1].start_ms == 9_500
    assert first[-1].end_ms == 10_000
    assert second[0].start_ms == 0
    assert second[0].end_ms == 500
    assert second[0].lines == first[-1].lines


def test_split_uses_actual_non_round_media_boundaries(tmp_path: Path) -> None:
    source = tmp_path / "chat.srt"
    source.write_text(REAL_CHAT, encoding="utf-8")
    outputs = [tmp_path / "part1.srt", tmp_path / "part2.srt"]
    windows = [
        SegmentWindow(1, 0.0, 9.8),
        SegmentWindow(2, 9.8, 20.0),
    ]

    results = split_srt_file(source, windows, outputs)

    assert [result.cue_count for result in results] == [2, 2]
    second = read_srt(outputs[1])
    assert second[0].start_ms == 0
    assert second[0].end_ms == 700
    assert outputs[0].read_bytes().startswith(b"1\n")


@pytest.mark.parametrize(
    "text",
    [
        "1\nnot a timestamp\ntext\n",
        "1\n00:00:02,000 --> 00:00:01,000\ntext\n",
        "1\n00:00:01,000 --> 00:00:02,000\n",
        "1\n00:99:01,000 --> 00:00:02,000\ntext\n",
    ],
)
def test_malformed_srt_is_rejected(text: str) -> None:
    with pytest.raises(SRTError):
        parse_srt_text(text)


def test_non_utf8_srt_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "broken.srt"
    source.write_bytes(b"\xff\xfe\x00\x01")

    with pytest.raises(SRTError, match="not valid UTF-8"):
        read_srt(source)


def test_empty_srt_creates_empty_segment_files(tmp_path: Path) -> None:
    source = tmp_path / "empty.srt"
    source.write_text("", encoding="utf-8")
    outputs = [tmp_path / "one.srt", tmp_path / "two.srt"]

    results = split_srt_file(
        source,
        [SegmentWindow(1, 0, 5), SegmentWindow(2, 5, 10)],
        outputs,
    )

    assert [result.cue_count for result in results] == [0, 0]
    assert all(path.read_text(encoding="utf-8") == "" for path in outputs)
