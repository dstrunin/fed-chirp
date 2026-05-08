"""Implied-rate math from the ZQ futures chain.

ZQ contract price = 100 - average daily effective fed funds rate over
the contract's calendar month. From a chain of monthly contracts plus
the FOMC meeting calendar, we recover the implied rate at each upcoming
meeting using a step-path: the rate is constant within a month except
on the meeting day, when it shifts to the new policy rate.

Standard CME FedWatch convention (the one we mirror):

    For meeting on day D in month M of length N:
        implied_avg(M) = (rate_before * (D-1) + rate_after * (N-D+1)) / N
    →   rate_after = (implied_avg(M) * N - rate_before * (D-1)) / (N-D+1)

Per-meeting move probabilities assume 25bp increments. The implied rate
change `delta` is decomposed into linear weights between the two nearest
25bp buckets — same simplification CME uses in their public FedWatch tool.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
from dataclasses import dataclass

# Bucket size for move-probability decomposition (basis points).
BUCKET_BP = 25.0
# Range of buckets to display; -100 to +100 covers the realistic range.
BUCKETS_BP = (-100.0, -75.0, -50.0, -25.0, 0.0, 25.0, 50.0, 75.0, 100.0)


@dataclass(frozen=True)
class MeetingRate:
    meeting_date: dt.date
    rate_before: float       # rate going into the meeting (%)
    rate_after: float        # rate after the meeting (%)
    delta_bp: float          # (rate_after - rate_before) * 100


@dataclass(frozen=True)
class MoveProbabilities:
    meeting_date: dt.date
    delta_bp: float
    # bucket_bp -> probability in [0, 1]
    buckets: dict[float, float]


def implied_rates_at_meetings(
    chain: dict[str, float],
    meetings: list[dt.date],
    current_rate: float,
) -> list[MeetingRate]:
    """Walk the chain forward month-by-month, solving for the post-meeting
    rate at each FOMC meeting.

    Args:
        chain: {"YYYY-MM": implied_avg_rate_pct, ...}
        meetings: ordered list of FOMC meeting dates (already filtered to
                  upcoming ones if you only want forward-looking)
        current_rate: current effective fed funds rate, in percent

    Returns one MeetingRate per meeting that falls inside the chain's
    coverage window. Meetings outside the chain are silently skipped.
    """
    out: list[MeetingRate] = []
    months_sorted = sorted(chain.keys())
    if not months_sorted:
        return out

    rate_running = current_rate

    # Index meetings by year-month for quick lookup.
    meetings_by_month: dict[str, list[dt.date]] = {}
    for m in meetings:
        key = f"{m.year:04d}-{m.month:02d}"
        meetings_by_month.setdefault(key, []).append(m)
    for k in meetings_by_month:
        meetings_by_month[k].sort()

    for month_str in months_sorted:
        avg = chain[month_str]
        year, mon = int(month_str[0:4]), int(month_str[5:7])
        n_days = calendar.monthrange(year, mon)[1]

        ms_in_month = meetings_by_month.get(month_str, [])

        if not ms_in_month:
            # Whole month is at one constant rate. The chain's implied avg
            # IS that rate. Update running rate from market.
            rate_running = avg
            continue

        # If multiple meetings fall in one month (rare, e.g. unscheduled),
        # treat them as a single composite move at the latest meeting day.
        meeting = ms_in_month[-1]
        d = meeting.day

        if d <= 0 or d > n_days:
            continue

        # rate_after is what we solve for; rate_before is the running rate
        # going into this month.
        if d == 1:
            # Meeting on day 1 → rate_after governs the whole month.
            rate_after = avg
        else:
            rate_after = (avg * n_days - rate_running * (d - 1)) / (n_days - d + 1)

        delta_bp = (rate_after - rate_running) * 100.0
        out.append(MeetingRate(
            meeting_date=meeting,
            rate_before=rate_running,
            rate_after=rate_after,
            delta_bp=delta_bp,
        ))
        rate_running = rate_after

    return out


def move_probabilities(meeting_rate: MeetingRate) -> MoveProbabilities:
    """Decompose an implied delta_bp into linear weights on the nearest
    two 25bp buckets. Outside the displayable range we clip to the edge
    bucket — should be rare for typical Fed cycles.
    """
    delta = meeting_rate.delta_bp
    # Clip to display range
    lo, hi = BUCKETS_BP[0], BUCKETS_BP[-1]
    delta_clipped = max(lo, min(hi, delta))

    # Find the two adjacent buckets that bracket delta.
    buckets = {b: 0.0 for b in BUCKETS_BP}
    if delta_clipped <= lo:
        buckets[lo] = 1.0
    elif delta_clipped >= hi:
        buckets[hi] = 1.0
    else:
        # find pair
        for i in range(len(BUCKETS_BP) - 1):
            a, b = BUCKETS_BP[i], BUCKETS_BP[i + 1]
            if a <= delta_clipped <= b:
                if math.isclose(a, b):
                    buckets[a] = 1.0
                else:
                    w_b = (delta_clipped - a) / (b - a)
                    w_a = 1.0 - w_b
                    buckets[a] = w_a
                    buckets[b] = w_b
                break

    return MoveProbabilities(
        meeting_date=meeting_rate.meeting_date,
        delta_bp=delta,
        buckets=buckets,
    )


def market_score(
    chain: dict[str, float],
    current_rate: float,
    horizon_months: int = 12,
) -> tuple[float, float]:
    """Convert the implied 12-month rate path into a -2..+2 hawk/dove
    score on the same scale as our Fed-speak score.

    Mapping: +100bp expected hikes → +1.0, -100bp → -1.0, clipped at ±2.

    Returns: (market_score, implied_bp_change_over_horizon)
    """
    if not chain:
        return 0.0, 0.0

    months_sorted = sorted(chain.keys())
    target_index = min(horizon_months - 1, len(months_sorted) - 1)
    if target_index < 0:
        return 0.0, 0.0

    # Implied rate at target month vs current rate
    target_rate = chain[months_sorted[target_index]]
    bp_change = (target_rate - current_rate) * 100.0
    score = max(-2.0, min(2.0, bp_change / 100.0))
    return score, bp_change


def current_rate_from_chain(
    chain: dict[str, float],
    meetings: list[dt.date],
    asof: dt.date | None = None,
) -> float | None:
    """Estimate the current effective fed funds rate from the chain.

    Strategy: find the earliest month in the chain that contains no FOMC
    meeting. The implied avg rate for that month equals the constant rate
    over the month, which is the current policy rate going into it.

    If the front month has a meeting (e.g. you're scanning during a
    meeting month), we walk forward until we hit the first no-meeting
    month. Should usually be within 1 month.
    """
    if not chain:
        return None
    asof = asof or dt.date.today()
    months_sorted = sorted(chain.keys())
    meeting_months = {f"{m.year:04d}-{m.month:02d}" for m in meetings if m >= asof}
    for month_str in months_sorted:
        # Skip months strictly before "now" — they're stale settlement.
        y, mn = int(month_str[0:4]), int(month_str[5:7])
        if dt.date(y, mn, 1) < dt.date(asof.year, asof.month, 1):
            continue
        if month_str not in meeting_months:
            return chain[month_str]
    # All months have meetings; fall back to the front month and accept
    # the small bias from intra-month meeting weighting.
    return chain[months_sorted[0]]


def implied_rate_n_months_out(
    chain: dict[str, float],
    n_months: int,
    asof: dt.date | None = None,
) -> float | None:
    """Return the implied avg rate for the contract month that is `n_months`
    forward from the current month (asof defaults to today).
    """
    asof = asof or dt.date.today()
    y, m = asof.year, asof.month
    m += n_months
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    key = f"{y:04d}-{m:02d}"
    return chain.get(key)
