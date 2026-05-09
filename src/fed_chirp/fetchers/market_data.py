"""Pull intraday OHLC bars for index futures from Yahoo Finance.

Used by the FOMC market-reaction tracker: the statement (2:00pm ET) and
press conference (2:30pm ET) anchor two windows whose moves we measure
on ES=F (E-mini S&P 500) and NQ=F (E-mini Nasdaq-100).

yfinance intraday-history limits drive the interval choice:
  - 1m  bars: only available for the last ~7 days
  - 5m  bars: last ~60 days
  - 15m bars: last ~60 days
  - 1h  bars: last ~730 days (~2 years)
Anything older returns no intraday rows. We pick the finest interval that
covers the requested range and let the analysis layer skip the 30-min
statement window when bars are too coarse to resolve it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import yfinance as yf

# Bars we expose. Order matters: finest first.
_INTERVAL_LIMITS_DAYS: list[tuple[str, int]] = [
    ("1m", 7),
    ("5m", 60),
    ("15m", 60),
    ("1h", 730),
]


@dataclass(frozen=True)
class IntradayBars:
    ticker: str
    interval: str
    bars: list[tuple[dt.datetime, float, float, float, float]]
    # each bar: (ts_utc, open, high, low, close)


def _pick_interval(start_utc: dt.datetime, now_utc: dt.datetime) -> str | None:
    """Finest yfinance interval still serving data back to `start_utc`.

    Returns None when even 1h bars are out of range (meeting > 2y old).
    """
    age_days = (now_utc - start_utc).total_seconds() / 86400.0
    for interval, max_days in _INTERVAL_LIMITS_DAYS:
        # Pad by 1 day to avoid boundary flakiness near the cutoff.
        if age_days <= max_days - 1:
            return interval
    return None


def fetch_window(
    ticker: str,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
) -> IntradayBars:
    """Fetch OHLC bars for `ticker` covering [start_utc, end_utc].

    Picks the finest interval yfinance still serves for this age. Returns
    timestamps in UTC. On error or no data, returns IntradayBars with an
    empty bars list (and a best-guess interval). Caller should treat empty
    as "skip this meeting".
    """
    now_utc = dt.datetime.now(dt.timezone.utc)
    interval = _pick_interval(start_utc, now_utc)
    if interval is None:
        return IntradayBars(ticker=ticker, interval="none", bars=[])

    # yfinance wants naive dates or tz-aware datetimes; pad ±1 day around
    # the requested window so timezone slop and the cutoff at end-of-day
    # never trims the bars we actually want.
    fetch_start = (start_utc - dt.timedelta(days=1)).date()
    fetch_end = (end_utc + dt.timedelta(days=2)).date()

    try:
        df = yf.download(
            tickers=ticker,
            start=fetch_start.isoformat(),
            end=fetch_end.isoformat(),
            interval=interval,
            prepost=True,
            progress=False,
            auto_adjust=False,
            group_by="ticker",
        )
    except Exception:
        return IntradayBars(ticker=ticker, interval=interval, bars=[])

    if df is None or df.empty:
        return IntradayBars(ticker=ticker, interval=interval, bars=[])

    # When called with a single ticker and group_by="ticker" yfinance still
    # returns a MultiIndex on columns (ticker -> field). Flatten to the
    # field level.
    if hasattr(df.columns, "levels") and len(df.columns.levels) > 1:
        try:
            df = df[ticker]
        except KeyError:
            df = df.droplevel(0, axis=1)

    if "Close" not in df.columns:
        return IntradayBars(ticker=ticker, interval=interval, bars=[])

    # Normalize index to UTC. yfinance intraday data is typically tz-aware
    # in the exchange timezone; daily-rolled data may come back tz-naive.
    idx = df.index
    if getattr(idx, "tz", None) is None:
        # Assume exchange-local time for futures (ET). Bars near DST flips
        # may be off by an hour but FOMC events never fall on DST changes.
        idx = idx.tz_localize("America/New_York", nonexistent="shift_forward",
                              ambiguous="NaT")
    idx = idx.tz_convert("UTC")

    bars: list[tuple[dt.datetime, float, float, float, float]] = []
    for ts, row in zip(idx, df.itertuples(index=False), strict=False):
        # Skip rows where the timestamp ended up NaT or any OHLC is NaN.
        try:
            o = float(row.Open)
            h = float(row.High)
            lo = float(row.Low)
            c = float(row.Close)
        except (TypeError, ValueError):
            continue
        if any(v != v for v in (o, h, lo, c)):  # NaN check
            continue
        try:
            py_ts = ts.to_pydatetime()
        except AttributeError:
            continue
        bars.append((py_ts, o, h, lo, c))

    bars.sort(key=lambda b: b[0])
    return IntradayBars(ticker=ticker, interval=interval, bars=bars)
