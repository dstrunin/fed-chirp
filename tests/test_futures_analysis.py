"""Tests for Fed funds futures path math."""

from __future__ import annotations

import datetime as dt

import pytest

from fed_chirp.analysis import futures


def test_current_rate_uses_front_month_when_next_meeting_is_later_this_month():
    chain = {
        "2026-06": 3.6225,
        "2026-07": 3.6250,
        "2026-08": 3.6600,
    }
    meetings = [dt.date(2026, 6, 17), dt.date(2026, 7, 29)]

    current = futures.current_rate_from_chain(chain, meetings, asof=dt.date(2026, 6, 9))

    assert current == chain["2026-06"]


def test_next_meeting_probabilities_are_based_on_front_month_current_rate():
    chain = {
        "2026-06": 3.6225,
        "2026-07": 3.6250,
        "2026-08": 3.6600,
    }
    meetings = [dt.date(2026, 6, 17), dt.date(2026, 7, 29)]
    current = futures.current_rate_from_chain(chain, meetings, asof=dt.date(2026, 6, 9))
    assert current is not None

    [meeting_rate] = futures.implied_rates_at_meetings(chain, meetings[:1], current)
    probs = futures.move_probabilities(meeting_rate)

    assert abs(meeting_rate.delta_bp) < 1.0
    assert probs.buckets[0.0] > 0.96
    assert probs.buckets[-25.0] < 0.001


def test_september_meeting_uses_observed_effr_not_blended_front_contract():
    chain = {
        "2026-09": 3.700,
        "2026-10": 3.790,
    }
    meetings = [dt.date(2026, 9, 16), dt.date(2026, 10, 28)]

    current = futures.current_rate_from_chain(
        chain,
        meetings,
        asof=dt.date(2026, 9, 9),
        observed_effr=3.63,
    )
    [meeting_rate] = futures.implied_rates_at_meetings(chain, meetings[:1], current)
    probs = futures.move_probabilities(meeting_rate)

    assert current == 3.63
    assert meeting_rate.delta_bp == pytest.approx(15.0)
    assert probs.buckets[0.0] == pytest.approx(0.40)
    assert probs.buckets[25.0] == pytest.approx(0.60)
