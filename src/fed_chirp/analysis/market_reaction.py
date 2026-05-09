"""Compute market-reaction metrics around FOMC events.

Two windows per meeting:
  - statement window: 2:00pm ET (statement) -> 2:30pm ET (presser)
  - post-presser:     2:30pm ET (presser)   -> +24h calendar hours

For each window we compute % change, annualized realized volatility,
high-low range, and the worst running drawdown vs the open.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..fetchers.market_data import IntradayBars

_ET = ZoneInfo("America/New_York")
_INTERVAL_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
_MINUTES_PER_YEAR = 525600  # 365 * 24 * 60


@dataclass(frozen=True)
class WindowMetrics:
    open: float
    close: float
    high: float
    low: float
    pct_change: float       # (close - open) / open * 100
    realized_vol: float     # annualized stdev of bar log returns, %
    range_pct: float        # (high - low) / open * 100
    max_move_pct: float     # max |running close - open| / open * 100, signed by direction


def fomc_event_times(meeting_date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """(statement_release_utc, presser_start_utc) for a meeting date.

    FRB convention since 2013: statements at 2:00pm ET, pressers at
    2:30pm ET. DST-aware via zoneinfo.
    """
    stmt_et = dt.datetime(
        meeting_date.year, meeting_date.month, meeting_date.day, 14, 0, tzinfo=_ET
    )
    presser_et = dt.datetime(
        meeting_date.year, meeting_date.month, meeting_date.day, 14, 30, tzinfo=_ET
    )
    return (
        stmt_et.astimezone(dt.timezone.utc),
        presser_et.astimezone(dt.timezone.utc),
    )


def cash_close_times(meeting_date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """(same_day_close_utc, next_trading_day_close_utc).

    Both anchored at 16:00 ET, the regular cash-session close. The next
    trading day skips Sat/Sun. FOMC meetings are always on Tue/Wed so
    the +1 day always lands on a weekday in practice; the skip is a
    belt-and-suspenders safety net.
    """
    eod_et = dt.datetime(
        meeting_date.year, meeting_date.month, meeting_date.day, 16, 0, tzinfo=_ET
    )
    nxt = meeting_date + dt.timedelta(days=1)
    while nxt.weekday() >= 5:  # Saturday=5, Sunday=6
        nxt += dt.timedelta(days=1)
    nextday_et = dt.datetime(nxt.year, nxt.month, nxt.day, 16, 0, tzinfo=_ET)
    return (
        eod_et.astimezone(dt.timezone.utc),
        nextday_et.astimezone(dt.timezone.utc),
    )


def _bars_in_window(
    bars: IntradayBars, start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, float, float, float, float]]:
    return [b for b in bars.bars if start <= b[0] < end]


def compute_window(
    bars: IntradayBars, start: dt.datetime, end: dt.datetime
) -> WindowMetrics | None:
    """Metrics for the half-open window [start, end). Returns None when
    the window has fewer than 2 bars (insufficient for a vol estimate)
    or when bar interval is too coarse to fit the window at all."""
    interval_min = _INTERVAL_MINUTES.get(bars.interval)
    if interval_min is None:
        return None

    window_minutes = (end - start).total_seconds() / 60.0
    # If the window is shorter than 2 bars, we can't measure it cleanly.
    # E.g. 30-min statement window on 1h bars has no full bars inside.
    if window_minutes < 2 * interval_min:
        return None

    rows = _bars_in_window(bars, start, end)
    if len(rows) < 2:
        return None

    opens = [r[1] for r in rows]
    highs = [r[2] for r in rows]
    lows = [r[3] for r in rows]
    closes = [r[4] for r in rows]

    open_px = opens[0]
    close_px = closes[-1]
    high_px = max(highs)
    low_px = min(lows)

    pct_change = (close_px - open_px) / open_px * 100.0
    range_pct = (high_px - low_px) / open_px * 100.0

    # Worst running excursion from open, signed by direction. If price
    # ever moved +0.8% but ended -0.2%, max_move is +0.8.
    excursions = [(c - open_px) / open_px * 100.0 for c in closes]
    max_move = max(excursions, key=abs) if excursions else 0.0

    # Annualized realized volatility from log returns of consecutive closes.
    log_rets: list[float] = []
    for prev, cur in zip(closes[:-1], closes[1:], strict=False):
        if prev > 0 and cur > 0:
            log_rets.append(math.log(cur / prev))
    if len(log_rets) < 2:
        realized_vol = 0.0
    else:
        mean = sum(log_rets) / len(log_rets)
        var = sum((x - mean) ** 2 for x in log_rets) / (len(log_rets) - 1)
        sd = math.sqrt(var)
        annual_factor = math.sqrt(_MINUTES_PER_YEAR / interval_min)
        realized_vol = sd * annual_factor * 100.0

    return WindowMetrics(
        open=open_px,
        close=close_px,
        high=high_px,
        low=low_px,
        pct_change=pct_change,
        realized_vol=realized_vol,
        range_pct=range_pct,
        max_move_pct=max_move,
    )
