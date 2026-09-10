"""Tests for official EFFR fetching."""

from __future__ import annotations

import datetime as dt

import pytest

from fed_chirp.fetchers import futures


def test_parse_latest_effr_uses_newest_official_observation():
    payload = {
        "refRates": [
            {"effectiveDate": "2026-09-08", "type": "EFFR", "percentRate": 3.63},
            {"effectiveDate": "2026-09-09", "type": "EFFR", "percentRate": 3.64},
        ]
    }

    observation = futures.parse_latest_effr(payload)

    assert observation.effective_date == dt.date(2026, 9, 9)
    assert observation.rate == 3.64


def test_parse_latest_effr_rejects_missing_observations():
    with pytest.raises(ValueError, match="EFFR"):
        futures.parse_latest_effr({"refRates": []})
