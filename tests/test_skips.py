"""Tests for the processing_skips transparency log."""

from __future__ import annotations

import datetime as dt

import pytest

from fed_chirp.storage.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.sqlite")


def test_record_and_recent_skip(db):
    db.record_skip(
        url="https://example.com/a",
        speaker_key="powell",
        pub_date=dt.date(2026, 5, 1),
        reason="rubric_excluded",
        message="non-MP topic",
    )
    out = db.recent_skips(since=dt.date(2026, 1, 1))
    assert len(out) == 1
    s = out[0]
    assert s.url == "https://example.com/a"
    assert s.speaker_key == "powell"
    assert s.pub_date == dt.date(2026, 5, 1)
    assert s.reason == "rubric_excluded"
    assert s.message == "non-MP topic"


def test_record_skip_is_idempotent(db):
    """Re-recording the same URL with a new reason replaces the old row."""
    db.record_skip("u", "powell", dt.date(2026, 5, 1), "fetch_failed", "first")
    db.record_skip("u", "powell", dt.date(2026, 5, 1), "captions_unavailable", "second")
    out = db.recent_skips(since=dt.date(2026, 1, 1))
    assert len(out) == 1
    assert out[0].reason == "captions_unavailable"
    assert out[0].message == "second"


def test_clear_skip_removes_row(db):
    db.record_skip("u", "powell", dt.date(2026, 5, 1), "fetch_failed", "x")
    db.clear_skip("u")
    assert db.recent_skips(since=dt.date(2026, 1, 1)) == []


def test_clear_skip_no_op_when_missing(db):
    db.clear_skip("never-recorded")  # should not raise
    assert db.recent_skips(since=dt.date(2026, 1, 1)) == []


def test_recent_skips_window(db):
    db.record_skip("old", "powell", dt.date(2025, 1, 1), "rubric_excluded", "x")
    db.record_skip("new", "powell", dt.date(2026, 4, 1), "rubric_excluded", "y")
    out = db.recent_skips(since=dt.date(2026, 1, 1))
    assert [s.url for s in out] == ["new"]


def test_recent_skips_sort_newest_first(db):
    db.record_skip("a", "x", dt.date(2026, 3, 1), "rubric_excluded", "")
    db.record_skip("b", "x", dt.date(2026, 5, 1), "rubric_excluded", "")
    db.record_skip("c", "x", dt.date(2026, 4, 1), "rubric_excluded", "")
    out = db.recent_skips(since=dt.date(2026, 1, 1))
    assert [s.url for s in out] == ["b", "c", "a"]


def test_skip_with_null_pub_date_excluded_from_window(db):
    """Skips lacking a pub_date can't be windowed cleanly — drop from view."""
    db.record_skip("u", "powell", None, "fetch_failed", "x")
    assert db.recent_skips(since=dt.date(2020, 1, 1)) == []
