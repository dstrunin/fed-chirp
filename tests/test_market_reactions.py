from __future__ import annotations

import datetime as real_dt

from fed_chirp import cli
from fed_chirp.fetchers.market_data import IntradayBars


class FakeDateTime(real_dt.datetime):
    now_value: real_dt.datetime

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.now_value.replace(tzinfo=None)
        return cls.now_value.astimezone(tz)


class FakeDB:
    def __init__(self, existing=None):
        self.inserted = []
        self.existing = existing or []

    def meeting_dates_with_presser(self):
        return [real_dt.date(2026, 6, 17)]

    def get_market_reactions(self):
        return list(self.existing)

    def insert_market_reaction(self, reaction):
        self.inserted.append(reaction)


def _bars(ticker: str) -> IntradayBars:
    start = real_dt.datetime(2026, 6, 17, 18, 0, tzinfo=real_dt.timezone.utc)
    rows = []
    price = 100.0
    for i in range(325):
        ts = start + real_dt.timedelta(minutes=5 * i)
        rows.append((ts, price, price + 0.2, price - 0.1, price + 0.1))
        price += 0.1
    return IntradayBars(ticker=ticker, interval="5m", bars=rows)


def test_market_reactions_populate_same_day_after_cash_close_before_nextday(monkeypatch):
    original_datetime = cli.dt.datetime
    FakeDateTime.now_value = real_dt.datetime(2026, 6, 17, 22, 0, tzinfo=real_dt.timezone.utc)
    monkeypatch.setattr(cli.dt, "datetime", FakeDateTime)
    monkeypatch.setattr(cli.market_data, "fetch_window", lambda ticker, start, end: _bars(ticker))

    db = FakeDB()
    try:
        cli._refresh_market_reactions(db)
    finally:
        monkeypatch.setattr(cli.dt, "datetime", original_datetime)

    assert len(db.inserted) == len(cli._REACTION_TICKERS)
    assert all(r.meeting_date == real_dt.date(2026, 6, 17) for r in db.inserted)
    assert all(r.stmt_pct_change is not None for r in db.inserted)
    assert all(r.eod_pct_change is not None for r in db.inserted)
    assert all(r.nextday_pct_change is None for r in db.inserted)


def test_market_reactions_refresh_partial_rows_after_nextday_close(monkeypatch):
    original_datetime = cli.dt.datetime
    FakeDateTime.now_value = real_dt.datetime(2026, 6, 17, 22, 0, tzinfo=real_dt.timezone.utc)
    monkeypatch.setattr(cli.dt, "datetime", FakeDateTime)
    monkeypatch.setattr(cli.market_data, "fetch_window", lambda ticker, start, end: _bars(ticker))

    partial_db = FakeDB()
    try:
        cli._refresh_market_reactions(partial_db)
    finally:
        monkeypatch.setattr(cli.dt, "datetime", original_datetime)

    FakeDateTime.now_value = real_dt.datetime(2026, 6, 19, 0, 0, tzinfo=real_dt.timezone.utc)
    monkeypatch.setattr(cli.dt, "datetime", FakeDateTime)
    monkeypatch.setattr(cli.market_data, "fetch_window", lambda ticker, start, end: _bars(ticker))

    db = FakeDB(existing=partial_db.inserted)
    try:
        cli._refresh_market_reactions(db)
    finally:
        monkeypatch.setattr(cli.dt, "datetime", original_datetime)

    assert len(db.inserted) == len(cli._REACTION_TICKERS)
