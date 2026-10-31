"""The retired market-reaction section must stay out of the public page."""
import datetime as dt
from dataclasses import fields

import pytest
from bs4 import BeautifulSoup

from fed_chirp.fetchers.federalreserve import Speaker
from fed_chirp.output.dashboard import render
from fed_chirp.storage.db import MarketReaction, StoredScore


@pytest.mark.parametrize("with_reactions", [False, True])
def test_market_reactions_section_and_navigation_are_removed(tmp_path, with_reactions):
    values: dict = {field.name: None for field in fields(MarketReaction)}
    now = dt.datetime(2026, 10, 28, 18, tzinfo=dt.timezone.utc)
    values.update(
        meeting_date=now.date(), ticker="ES=F", bar_interval="5m",
        statement_release_dt=now, presser_start_dt=now,
        eod_close_dt=now, nextday_close_dt=now + dt.timedelta(days=1),
        fetched_at=now, stmt_pct_change=0.5,
    )
    output = tmp_path / "index.html"
    speaker = Speaker("test", "Test Governor", "Governor", None, ())
    score = StoredScore(
        speech_url="https://example.com/statement", speaker_key="fomc",
        speech_date=now.date(), score=0.5, label="neutral", rationale="Test",
        key_quotes=[], model="test", scored_at=now, doc_type="fomc_statement",
    )
    render([speaker], [score], output,
           reactions=[MarketReaction(**values)] if with_reactions else [])
    page = BeautifulSoup(output.read_text(), "html.parser")
    assert page.select_one("#reactions") is None
    assert not page.select('a[href="#reactions"]')
    assert "Where rates and" not in page.get_text(" ", strip=True)
    assert page.select_one("#fomc") is not None
    assert page.select_one("#committee") is not None
