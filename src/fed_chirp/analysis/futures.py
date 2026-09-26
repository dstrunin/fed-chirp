"""Implied-rate math from the ZQ futures chain.

ZQ contract price = 100 - average daily effective fed funds rate over
the contract's calendar month. From a chain of monthly contracts plus
the FOMC meeting calendar, we recover the implied rate at each upcoming
meeting using a step-path: the rate is constant within a month except
on the meeting day, when it shifts to the new policy rate.

Standard CME FedWatch convention (the one we mirror):

    For meeting decision on day D in month M of length N, the new target
    takes effect the following day:
        implied_avg(M) = (rate_before * D + rate_after * (N-D)) / N
    →   rate_after = (implied_avg(M) * N - rate_before * D) / (N-D)

Months without a meeting are rate anchors. Their implied average pins the
end rate of the preceding meeting month and the start rate of the following
meeting month. Meeting months are then solved backward and forward from
those anchors; observed EFFR is only a fallback when no anchor is available.

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
    known_meetings: list[dt.date] | None = None,
) -> list[MeetingRate]:
    """Bootstrap meeting rates from no-meeting-month anchors.

    Args:
        chain: {"YYYY-MM": implied_avg_rate_pct, ...}
        meetings: ordered list of FOMC meeting dates (already filtered to
                  upcoming ones if you only want forward-looking)
        current_rate: observed effective fed funds rate, used only if the
                      available chain cannot anchor a meeting
        known_meetings: complete calendar, including recently completed
                        meetings, used to distinguish true anchor months

    Returns one MeetingRate per meeting that falls inside the chain's
    coverage window. Meetings outside the chain are silently skipped.
    """
    months_sorted = sorted(chain.keys())
    if not months_sorted or not meetings:
        return []

    # Index meetings by year-month for quick lookup.
    meetings_by_month: dict[str, list[dt.date]] = {}
    for m in known_meetings or meetings:
        key = f"{m.year:04d}-{m.month:02d}"
        meetings_by_month.setdefault(key, []).append(m)
    for m in meetings:
        key = f"{m.year:04d}-{m.month:02d}"
        if m not in meetings_by_month.setdefault(key, []):
            meetings_by_month[key].append(m)
    for k in meetings_by_month:
        meetings_by_month[k].sort()
    first_meeting = min(meetings)
    first_meeting_month = f"{first_meeting.year:04d}-{first_meeting.month:02d}"

    def adjacent_month(month_str: str, offset: int) -> str:
        year, mon = int(month_str[:4]), int(month_str[5:7])
        mon += offset
        if mon == 0:
            year, mon = year - 1, 12
        elif mon == 13:
            year, mon = year + 1, 1
        return f"{year:04d}-{mon:02d}"

    anchor_search_start = adjacent_month(first_meeting_month, -1)
    relevant_months = [m for m in months_sorted if m >= anchor_search_start]

    starts: dict[str, float] = {}
    ends: dict[str, float] = {}

    # A full month without an FOMC meeting prices a single flat rate. It pins
    # the adjacent meeting month(s), which is CME FedWatch's anchor rule.
    for month_str in relevant_months:
        if month_str in meetings_by_month:
            continue
        avg = chain[month_str]
        prev_month = adjacent_month(month_str, -1)
        next_month = adjacent_month(month_str, 1)
        if prev_month in meetings_by_month:
            ends.setdefault(prev_month, avg)
        if next_month in meetings_by_month:
            starts.setdefault(next_month, avg)

    # Propagate anchor information through consecutive meeting months. Work
    # backward first, matching CME's precedence for no-meeting anchors.
    changed = True
    while changed:
        changed = False
        for month_str in reversed(relevant_months):
            if month_str not in meetings_by_month or month_str in starts:
                continue
            if month_str not in ends:
                continue
            meeting = meetings_by_month[month_str][-1]
            n_days = calendar.monthrange(meeting.year, meeting.month)[1]
            pre_days, post_days = meeting.day, n_days - meeting.day
            if month_str not in chain or pre_days <= 0:
                continue
            starts[month_str] = (
                chain[month_str] * n_days - post_days * ends[month_str]
            ) / pre_days
            prev_month = adjacent_month(month_str, -1)
            if prev_month in meetings_by_month:
                ends.setdefault(prev_month, starts[month_str])
            changed = True

        for month_str in relevant_months:
            if (
                month_str not in meetings_by_month
                or month_str in ends
                or month_str not in starts
                or month_str not in chain
            ):
                continue
            meeting = meetings_by_month[month_str][-1]
            n_days = calendar.monthrange(meeting.year, meeting.month)[1]
            pre_days, post_days = meeting.day, n_days - meeting.day
            if post_days <= 0:
                continue
            ends[month_str] = (
                chain[month_str] * n_days - pre_days * starts[month_str]
            ) / post_days
            next_month = adjacent_month(month_str, 1)
            if next_month in meetings_by_month:
                starts.setdefault(next_month, ends[month_str])
            changed = True

    # Fall back to observed EFFR only for a leading meeting that the available
    # chain could not connect to a no-meeting anchor.
    rate_running = current_rate
    for month_str in relevant_months:
        if month_str < first_meeting_month:
            continue
        if month_str not in meetings_by_month:
            rate_running = chain[month_str]
            continue
        meeting = meetings_by_month[month_str][-1]
        n_days = calendar.monthrange(meeting.year, meeting.month)[1]
        pre_days, post_days = meeting.day, n_days - meeting.day
        if month_str not in starts:
            starts[month_str] = rate_running
        if month_str not in ends and month_str in chain and post_days > 0:
            ends[month_str] = (
                chain[month_str] * n_days - pre_days * starts[month_str]
            ) / post_days
        if month_str in ends:
            rate_running = ends[month_str]

    out: list[MeetingRate] = []
    for meeting in sorted(meetings):
        month_str = f"{meeting.year:04d}-{meeting.month:02d}"
        if month_str not in starts or month_str not in ends:
            continue
        rate_before, rate_after = starts[month_str], ends[month_str]
        out.append(MeetingRate(
            meeting_date=meeting,
            rate_before=rate_before,
            rate_after=rate_after,
            delta_bp=(rate_after - rate_before) * 100.0,
        ))
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
    observed_effr: float | None = None,
) -> float | None:
    """Return the observed EFFR, falling back to a chain estimate.

    The current-month futures contract is a monthly average. When an FOMC
    meeting remains in that month it blends pre- and post-meeting rates, so it
    cannot serve as the current rate for solving that meeting's probabilities.
    """
    if observed_effr is not None:
        return observed_effr
    if not chain:
        return None
    asof = asof or dt.date.today()
    months_sorted = sorted(chain.keys())
    current_month = f"{asof.year:04d}-{asof.month:02d}"
    if current_month in chain:
        return chain[current_month]

    for month_str in months_sorted:
        # Skip months strictly before "now" — they're stale settlement.
        y, mn = int(month_str[0:4]), int(month_str[5:7])
        if dt.date(y, mn, 1) >= dt.date(asof.year, asof.month, 1):
            return chain[month_str]
    return chain[months_sorted[-1]]


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
