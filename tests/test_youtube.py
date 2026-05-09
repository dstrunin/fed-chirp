"""Tests for YouTube URL parsing and segment-to-paragraph chunking.

Network-touching `fetch_transcript` is not exercised here — that path is
verified manually against real Atlanta Fed RSS items.
"""

from __future__ import annotations

from dataclasses import dataclass

from fed_chirp.fetchers.youtube import (
    extract_video_id,
    is_youtube_url,
    _segments_to_paragraphs,
)


def test_is_youtube_url():
    assert is_youtube_url("https://www.youtube.com/live/abc12345678")
    assert is_youtube_url("https://youtube.com/watch?v=abc12345678")
    assert is_youtube_url("https://youtu.be/abc12345678")
    assert is_youtube_url("https://m.youtube.com/watch?v=abc12345678")
    assert not is_youtube_url("https://www.atlantafed.org/news-and-events/speeches/2026/01/01/foo")
    assert not is_youtube_url("https://farmjournaltv.com/programs/atlanta-fed")


def test_extract_video_id_from_live_path():
    assert extract_video_id("https://www.youtube.com/live/izmOhgLd_b4") == "izmOhgLd_b4"
    assert extract_video_id("https://www.youtube.com/live/-E4dw7pCIbI") == "-E4dw7pCIbI"


def test_extract_video_id_from_watch():
    assert extract_video_id("https://youtube.com/watch?v=abc12345678") == "abc12345678"
    assert extract_video_id("https://www.youtube.com/watch?v=abc12345678&t=42") == "abc12345678"


def test_extract_video_id_from_short():
    assert extract_video_id("https://youtu.be/abc12345678") == "abc12345678"
    assert extract_video_id("https://youtu.be/abc12345678?si=xyz") == "abc12345678"


def test_extract_video_id_from_embed():
    assert extract_video_id("https://www.youtube.com/embed/abc12345678") == "abc12345678"


def test_extract_video_id_returns_none_for_non_youtube():
    assert extract_video_id("https://atlantafed.org/news") is None


@dataclass
class _Seg:
    text: str


def test_segments_to_paragraphs_chunks_short_lines():
    # 100 short segments of ~10 chars each; should chunk into multiple
    # paragraphs of ~500 chars instead of one segment per line.
    segs = [_Seg(f"word {i:03d} ") for i in range(100)]
    out = _segments_to_paragraphs(segs)
    paragraphs = out.split("\n\n")
    # Many segments per paragraph — paragraph count should be far less than 100.
    assert len(paragraphs) < 30
    # Each paragraph (except possibly the last) has at least the target size.
    for p in paragraphs[:-1]:
        assert len(p) >= 400


def test_segments_to_paragraphs_skips_empty():
    segs = [_Seg("hello "), _Seg(""), _Seg("   "), _Seg("world ")]
    out = _segments_to_paragraphs(segs)
    assert "hello" in out and "world" in out
    assert "  " not in out  # no double-space artifacts from empty entries
