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


def test_september_meeting_uses_october_no_meeting_anchor():
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
    assert meeting_rate.rate_before == pytest.approx(3.62125)
    assert meeting_rate.rate_after == pytest.approx(3.79)
    assert meeting_rate.delta_bp == pytest.approx(16.875)
    assert probs.buckets[0.0] == pytest.approx(0.325)
    assert probs.buckets[25.0] == pytest.approx(0.675)


def test_october_rollover_uses_next_no_meeting_month_as_cme_anchor():
    """Reproduce CME's published Oct. 25 close: 35.8% hold / 64.2% hike."""
    chain = {
        "2026-09": 3.7475,  # blended pre/post September 16 meeting average
        "2026-10": 3.8900,
        "2026-11": 4.0350,
    }
    upcoming_meetings = [dt.date(2026, 10, 28)]

    [october] = futures.implied_rates_at_meetings(
        chain,
        upcoming_meetings,
        current_rate=3.88,
        known_meetings=[dt.date(2026, 9, 16), *upcoming_meetings],
    )
    probs = futures.move_probabilities(october)

    assert october.rate_before == pytest.approx(3.8744642857)
    assert october.rate_after == pytest.approx(4.035)
    assert october.delta_bp == pytest.approx(16.0535714286)
    assert probs.buckets[0.0] == pytest.approx(0.3578571429)
    assert probs.buckets[25.0] == pytest.approx(0.6421428571)
    assert probs.buckets[-25.0] == 0.0


def test_observed_effr_is_fallback_when_no_anchor_contract_is_available():
    [october] = futures.implied_rates_at_meetings(
        {"2026-10": 3.89},
        [dt.date(2026, 10, 28)],
        current_rate=3.88,
    )

    assert october.rate_before == pytest.approx(3.88)
    assert october.rate_after == pytest.approx(3.9833333333)


def test_previous_no_meeting_month_anchors_after_calendar_rollover():
    meetings = [dt.date(2026, 12, 9), dt.date(2027, 1, 27)]
    rates = futures.implied_rates_at_meetings(
        {
            "2026-11": 4.035,
            "2026-12": 4.175,
            "2027-01": 4.245,
            "2027-02": 4.355,
        },
        meetings,
        current_rate=4.00,
        known_meetings=meetings,
    )

    assert rates[0].meeting_date == dt.date(2026, 12, 9)
    assert rates[0].rate_before == pytest.approx(4.035)
    assert rates[1].rate_after == pytest.approx(4.355)
