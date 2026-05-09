"""Tests for coverage-health stale-speaker detection."""

from __future__ import annotations

import datetime as dt

from fed_chirp.analysis.health import find_stale
from fed_chirp.fetchers.federalreserve import Speaker


def _sp(key: str, name: str = "Test") -> Speaker:
    return Speaker(
        key=key, name=name, role="role", rss=None, aliases=(),
        region="Test Region", source="frb_board",
    )


ASOF = dt.date(2026, 5, 9)


def test_fresh_speaker_not_flagged():
    sp = _sp("waller")
    last = {"waller": dt.date(2026, 5, 5)}  # 4 days ago
    assert find_stale([sp], last, ASOF, threshold_days=60) == []


def test_stale_speaker_flagged():
    sp = _sp("bostic")
    last = {"bostic": dt.date(2025, 11, 12)}  # ~178 days ago
    out = find_stale([sp], last, ASOF, threshold_days=60)
    assert len(out) == 1
    assert out[0].speaker.key == "bostic"
    assert out[0].last_speech_date == dt.date(2025, 11, 12)
    assert out[0].days_silent == (ASOF - dt.date(2025, 11, 12)).days


def test_speaker_never_seen_flagged():
    sp = _sp("ghost")
    out = find_stale([sp], {}, ASOF, threshold_days=60)
    assert len(out) == 1
    assert out[0].last_speech_date is None
    assert out[0].days_silent == 10**6


def test_threshold_boundary():
    sp = _sp("edge")
    # Exactly 60 days ago — NOT flagged (gap > threshold required).
    last = {"edge": ASOF - dt.timedelta(days=60)}
    assert find_stale([sp], last, ASOF, threshold_days=60) == []
    # 61 days ago — flagged.
    last = {"edge": ASOF - dt.timedelta(days=61)}
    assert len(find_stale([sp], last, ASOF, threshold_days=60)) == 1


def test_results_sorted_most_stale_first():
    a = _sp("a"); b = _sp("b"); c = _sp("c")
    last = {
        "a": ASOF - dt.timedelta(days=70),
        "b": ASOF - dt.timedelta(days=200),
        "c": ASOF - dt.timedelta(days=120),
    }
    out = find_stale([a, b, c], last, ASOF, threshold_days=60)
    assert [s.speaker.key for s in out] == ["b", "c", "a"]


def test_never_seen_sorts_above_long_silent():
    a = _sp("a"); ghost = _sp("ghost")
    last = {"a": ASOF - dt.timedelta(days=400)}
    out = find_stale([a, ghost], last, ASOF, threshold_days=60)
    assert out[0].speaker.key == "ghost"
