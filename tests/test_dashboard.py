from __future__ import annotations

import datetime as dt

import pytest
from bs4 import BeautifulSoup

from fed_chirp.fetchers.federalreserve import Speaker
from fed_chirp.output import dashboard
from fed_chirp.storage.db import ProcessingSkip, StoredScore


@pytest.fixture
def rendered_page(tmp_path):
    today = dt.date.today()
    speaker = Speaker("test", "Test Governor", "Governor", None, ())
    scores = [
        StoredScore(
            speech_url=f"https://example.com/{kind}",
            speaker_key="test" if kind == "speech" else "fomc",
            speech_date=today,
            score=score,
            label="neutral",
            rationale="Test rationale",
            key_quotes=[],
            model="test",
            scored_at=dt.datetime.now(dt.timezone.utc),
            doc_type=kind,
        )
        for kind, score in [("speech", 0.5), ("fomc_statement", 0.5), ("fomc_presser", -0.5)]
    ]
    skip = ProcessingSkip(
        "https://example.com/skipped", "test", today, "fetch_failed", "Test failure",
        dt.datetime.now(dt.timezone.utc),
    )
    output = tmp_path / "index.html"
    dashboard.render([speaker], scores, output, skips=[skip])
    return BeautifulSoup(output.read_text(), "html.parser")


def test_qa_copy_is_chair_neutral(rendered_page):
    text = rendered_page.get_text(" ", strip=True)
    assert "Chair Q&A" in text
    assert "Powell" not in text


def test_speakers_section_and_links_are_removed(rendered_page):
    assert rendered_page.select_one("#speakers") is None
    assert not rendered_page.select('a[href="#speakers"]')


def test_transparency_is_not_rendered_even_with_skips(rendered_page):
    assert rendered_page.select_one("#transparency") is None
    assert "score, and why." not in rendered_page.get_text(" ", strip=True)


def test_committee_heading_is_hawks_vs_doves(rendered_page):
    heading = rendered_page.select_one("#committee h2")
    assert heading.get_text(" ", strip=True) == "Hawks vs. Doves"


def test_footer_coverage_links_point_to_committee(rendered_page):
    for link in rendered_page.select("footer a"):
        if link.get_text(strip=True).startswith(("Board governors", "Regional presidents")):
            assert link["href"] == "#committee"
