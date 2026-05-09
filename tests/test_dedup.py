"""Tests for content-hash dedup at the storage layer."""

from __future__ import annotations

import datetime as dt

import pytest

from fed_chirp.storage.db import Database, StoredSpeech, content_hash


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.sqlite")


def _speech(url: str, *, speaker="powell", date=None, body="Hello world"):
    return StoredSpeech(
        url=url,
        speaker_key=speaker,
        speech_date=date or dt.date(2026, 5, 1),
        title="Test Speech",
        location="Somewhere",
        body=body,
    )


def test_same_body_two_urls_dedupes(db):
    body = "This is a real speech body about monetary policy and inflation."
    canonical_url = db.insert_speech(_speech("https://x.com/2026/sp-aaa", body=body))
    second_url = db.insert_speech(_speech("https://x.com/sp-aaa", body=body))

    assert canonical_url == "https://x.com/2026/sp-aaa"
    assert second_url == canonical_url, "duplicate should return existing canonical URL"

    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM speeches").fetchone()["n"]
    assert n == 1


def test_different_bodies_keep_both(db):
    a = db.insert_speech(_speech("https://x.com/a", body="First speech body"))
    b = db.insert_speech(_speech("https://x.com/b", body="Second different body"))

    assert a == "https://x.com/a"
    assert b == "https://x.com/b"
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM speeches").fetchone()["n"]
    assert n == 2


def test_same_body_different_speaker_keep_both(db):
    body = "shared body text"
    a = db.insert_speech(_speech("https://x.com/a", speaker="powell", body=body))
    b = db.insert_speech(_speech("https://x.com/b", speaker="waller", body=body))

    assert a != b
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM speeches").fetchone()["n"]
    assert n == 2


def test_same_body_different_date_keep_both(db):
    body = "shared body text"
    a = db.insert_speech(_speech("https://x.com/a", date=dt.date(2026, 5, 1), body=body))
    b = db.insert_speech(_speech("https://x.com/b", date=dt.date(2026, 5, 2), body=body))

    assert a != b
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM speeches").fetchone()["n"]
    assert n == 2


def test_content_hash_normalization():
    # Whitespace and case differences collapse to the same hash.
    assert content_hash("Hello  World") == content_hash("hello world")
    assert content_hash("\n\nHello\tworld\n") == content_hash("hello world")
    assert content_hash("a b c") != content_hash("a b d")


def test_reinsert_same_url_overwrites_in_place(db):
    """Inserting the same URL twice should update the row, not raise."""
    url = "https://x.com/a"
    db.insert_speech(_speech(url, body="initial"))
    canonical = db.insert_speech(_speech(url, body="updated"))
    assert canonical == url
    stored = db.get_speech(url)
    assert stored is not None
    assert stored.body == "updated"
