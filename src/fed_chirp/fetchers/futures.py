"""Pull the 30-day Fed Funds (ZQ) futures chain from Yahoo Finance.

Each ZQ contract trades at price = 100 - average daily effective fed funds
rate over its contract month, so the chain of monthly contracts encodes
the market's expected path of fed funds rate changes (with discrete steps
on FOMC meeting dates).

Yahoo lists individual contracts as `ZQ<MonthCode><YY>.CBT` — e.g.
`ZQM26.CBT` is the June 2026 contract. We fetch the next ~14 monthly
contracts in a single batched call.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import yfinance as yf

# Futures month codes (CME convention).
_MONTH_CODES = ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]

CHAIN_LENGTH = 14  # months ahead


@dataclass(frozen=True)
class Settlement:
    contract_symbol: str       # e.g. ZQM26.CBT
    contract_month: str        # ISO YYYY-MM
    settle_date: dt.date
    settle_price: float
    implied_rate: float        # 100 - settle_price


def chain_symbols(start: dt.date | None = None, length: int = CHAIN_LENGTH) -> list[str]:
    """Build the ordered list of ZQ symbols for `length` months starting at
    the month containing `start` (defaults to today)."""
    start = start or dt.date.today()
    out: list[str] = []
    y, m = start.year, start.month
    for _ in range(length):
        out.append(f"ZQ{_MONTH_CODES[m - 1]}{y % 100:02d}.CBT")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def symbol_to_month(symbol: str) -> str:
    """Parse 'ZQM26.CBT' → '2026-06' (ISO YYYY-MM)."""
    # Symbol format: ZQ<MonthCode><YY>.<exchange>
    if not symbol.startswith("ZQ") or "." not in symbol:
        raise ValueError(f"Not a ZQ symbol: {symbol!r}")
    core = symbol[2:].split(".", 1)[0]  # e.g. M26
    code = core[0]
    yy = int(core[1:])
    if code not in _MONTH_CODES:
        raise ValueError(f"Unknown month code in {symbol!r}")
    month = _MONTH_CODES.index(code) + 1
    # Two-digit year: 00-79 → 2000s, 80-99 → 1900s. ZQ contracts only go
    # forward, so this is just a forward-looking heuristic.
    year = 2000 + yy if yy < 80 else 1900 + yy
    return f"{year:04d}-{month:02d}"


def fetch_chain(symbols: list[str]) -> list[Settlement]:
    """Fetch the latest settle for each ZQ contract via yfinance.download.

    Returns one Settlement per symbol that returned data. Symbols with no
    data are silently skipped — illiquid back-month contracts often have
    days with no settlement, especially on Mondays after a long weekend.
    """
    df = yf.download(
        symbols,
        period="5d",         # buffer for weekends/holidays
        progress=False,
        auto_adjust=False,
        group_by="ticker",
    )

    out: list[Settlement] = []
    for sym in symbols:
        try:
            sub = df[sym]
        except (KeyError, AttributeError):
            continue
        if "Close" not in sub.columns:
            continue
        s = sub["Close"].dropna()
        if s.empty:
            continue
        last_ts = s.index[-1]
        last_price = float(s.iloc[-1])
        settle_date = (
            last_ts.date() if hasattr(last_ts, "date") else dt.date.fromisoformat(str(last_ts)[:10])
        )
        out.append(
            Settlement(
                contract_symbol=sym,
                contract_month=symbol_to_month(sym),
                settle_date=settle_date,
                settle_price=last_price,
                implied_rate=100.0 - last_price,
            )
        )
    return out
