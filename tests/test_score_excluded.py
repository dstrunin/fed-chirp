"""Tests for the null/excluded score path."""

from __future__ import annotations

import datetime as dt

import pytest

from fed_chirp.scoring.scorer import ScoreResult
from fed_chirp.storage.db import Database, StoredSpeech


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.sqlite")


def _stored_speech(url="https://x.com/a", body="some body"):
    return StoredSpeech(
        url=url,
        speaker_key="powell",
        speech_date=dt.date(2026, 5, 1),
        title="Test",
        location="",
        body=body,
    )


def test_score_result_accepts_none():
    r = ScoreResult(
        score=None,
        label="excluded",
        rationale="page is empty",
        key_quotes=[],
        model="gpt-5.5",
        scored_at=dt.datetime.now(dt.timezone.utc),
    )
    assert r.score is None
    assert r.label == "excluded"


def test_insert_score_none_is_no_op(db):
    url = db.insert_speech(_stored_speech())
    db.insert_score(
        speech_url=url,
        score=None,
        label="excluded",
        rationale="page was empty",
        key_quotes=[],
        model="test-model",
        scored_at=dt.datetime.now(dt.timezone.utc),
    )
    assert db.has_score(url) is False


def test_insert_score_real_value_writes_row(db):
    url = db.insert_speech(_stored_speech())
    db.insert_score(
        speech_url=url,
        score=0.5,
        label="hawkish",
        rationale="r",
        key_quotes=["q"],
        model="test-model",
        scored_at=dt.datetime.now(dt.timezone.utc),
    )
    assert db.has_score(url) is True
