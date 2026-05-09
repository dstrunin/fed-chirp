"""Tests for the speech-likeness filter."""

from __future__ import annotations

from fed_chirp.scoring.speech_filter import evaluate


def test_empty_body_fails():
    r = evaluate("", doc_type="speech")
    assert not r.passes
    assert r.reason == "empty"


def test_whitespace_only_fails():
    r = evaluate("   \n\n\t  ", doc_type="speech")
    assert not r.passes
    assert r.reason == "empty"


def test_short_body_fails_even_with_keywords():
    # Roughly 100 words with policy keywords — should still fail length gate.
    body = (
        "Inflation has been elevated and the FOMC is monitoring labor market "
        "conditions. Monetary policy remains restrictive while we watch employment "
        "and prices. The federal funds rate is the primary tool. " * 5
    )
    r = evaluate(body, doc_type="speech")
    assert not r.passes
    assert r.reason == "too_short"


def test_long_body_no_keywords_still_passes():
    # Long substantive prose with no policy keywords passes the filter.
    # Topic relevance is the RUBRIC's job, not the filter's. A community-
    # development speech is still a speech and should be scored (likely 0.0).
    paragraph = (
        "Today I want to discuss the importance of community development across "
        "the regions we serve. Many small businesses and families benefit from "
        "the work that local banks and nonprofit organizations do every day. "
        "Investments in education and workforce training pay long-term dividends "
        "for the broader economy. We have seen meaningful progress in many "
        "communities thanks to the dedication of partners across sectors. "
    )
    body = paragraph * 10
    r = evaluate(body, doc_type="speech")
    assert r.passes, f"expected pass, got reason={r.reason}"


def test_nav_text_pattern_fails():
    # Musalem-shaped scrape: list of FRB tools, lots of short lines.
    body = "\n".join(
        ["FRED", "FRASER", "ALFRED", "CASSIDI", "Economic Data", "Research"]
        * 100
    )
    r = evaluate(body, doc_type="speech")
    assert not r.passes
    # With keyword gate removed, this should trip nav_text (or too_short
    # if the body is short enough).
    assert r.reason in {"nav_text", "too_short"}


def test_real_speech_passes():
    # 600+ words of policy-prose (paragraphs, ≥2 keywords, low link density).
    paragraph = (
        "Today I want to discuss the path of monetary policy and the outlook "
        "for inflation and the labor market. Recent data on prices have been "
        "encouraging, with disinflation continuing across both goods and services "
        "categories. The federal funds rate remains in restrictive territory, "
        "and I believe further patience is warranted before any rate cut. "
        "Employment has cooled but the unemployment rate remains low by "
        "historical standards, suggesting the dual mandate is broadly in balance. "
        "Wage growth has moderated alongside the broader cooling in the labor "
        "market, which should support continued progress on price stability. "
        "I will continue to weigh the risks in both directions as we calibrate "
        "the appropriate stance of policy. "
    )
    body = paragraph * 6  # ~600+ words
    r = evaluate(body, doc_type="speech")
    assert r.passes, f"expected pass, got reason={r.reason} wc={r.word_count} kw={r.keyword_hits}"
    assert r.reason is None


def test_fomc_doc_bypasses_filter():
    # Short body that would fail the speech filter, but doc_type bypasses.
    r = evaluate("short", doc_type="fomc_statement")
    assert r.passes
    assert r.reason is None
    r = evaluate("", doc_type="fomc_minutes")
    assert r.passes
    r = evaluate("", doc_type="fomc_presser")
    assert r.passes


def test_link_density_trips_nav_text():
    # 600 words with required keywords BUT layout is nav-list shaped:
    # mostly very short lines.
    short_lines = "\n".join(
        ["FOMC", "FRED", "FRASER", "rates", "inflation", "policy"] * 200
    )
    r = evaluate(short_lines, doc_type="speech")
    assert not r.passes
    # Expect nav_text once keyword and word-count gates have passed.
    assert r.reason == "nav_text"
