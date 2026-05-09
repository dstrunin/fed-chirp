"""Discover and fetch Federal Reserve Board governor speeches.

Discovery uses each governor's per-speaker RSS feed (one feed per governor,
listed at https://www.federalreserve.gov/feeds/feeds.htm). Per-speech fetch
parses the HTML at the URL given by the feed and pulls structured fields out
of the `div#article` container that's consistent across the FRB site.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

USER_AGENT = "fed-chirp/0.1 (+https://github.com/local; personal monitor)"
REQUEST_TIMEOUT = 30


@dataclass(frozen=True)
class Speaker:
    key: str
    name: str
    role: str
    rss: str | None  # None for synthetic speakers (e.g., "fomc") and non-RSS regionals
    aliases: tuple[str, ...]
    region: str = "Board"           # "Board", "Boston", "New York", ... — dashboard column
    source: str = "frb_board"       # "frb_board" | "fomc" | "regional_rss_html" | "regional_html" | "regional_js"
    index_url: str | None = None    # listing-page URL for non-RSS regionals
    tenure_start: dt.date | None = None  # drop discovered speeches dated before this


@dataclass
class SpeechRef:
    """Lightweight pointer returned by RSS / index-page discovery."""
    url: str
    speaker_key: str
    title: str
    pub_date: dt.date
    source: str = "frb_board"       # carried over from Speaker so fetch can dispatch


@dataclass
class Speech:
    """Fully-fetched speech ready for scoring/storage."""
    url: str
    speaker_key: str
    speech_date: dt.date
    title: str
    location: str
    body: str


def load_speakers(config_path: str | Path) -> list[Speaker]:
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    return [
        Speaker(
            key=s["key"],
            name=s["name"],
            role=s["role"],
            rss=s["rss"],
            aliases=tuple(a.lower() for a in s.get("aliases", [])),
            region=s.get("region", "Board"),
            source=s.get("source", "frb_board"),
            index_url=s.get("index_url"),
            tenure_start=_coerce_date(s.get("tenure_start")),
        )
        for s in raw["speakers"]
    ]


def _coerce_date(v) -> dt.date | None:
    if v is None:
        return None
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


def discover(
    speakers: list[Speaker],
    since: dt.date | None = None,
    *,
    pw=None,
) -> list[SpeechRef]:
    """Fetch all per-speaker speech indexes and return SpeechRefs.

    Dispatches by `speaker.source`:
      - "frb_board": existing per-speaker RSS feeds on federalreserve.gov
      - "regional_*": delegated to fetchers/regional.py
      - "fomc": skipped (FOMC docs use fetchers/fomc.py)

    `since` filters by pub_date >= since when provided. `pw` is an optional
    Playwright context (for regional_js banks); ignored otherwise.
    """
    frb_board = [s for s in speakers if s.source == "frb_board"]
    regional = [s for s in speakers if s.source.startswith("regional_")]

    refs: list[SpeechRef] = list(_discover_frb_board(frb_board, since))
    if regional:
        from . import regional as regional_mod
        refs.extend(regional_mod.discover_regional(regional, since, pw=pw))
    return refs


def _discover_frb_board(
    speakers: list[Speaker], since: dt.date | None
) -> list[SpeechRef]:
    """Original per-speaker RSS discovery for FRB Board governors."""
    refs: list[SpeechRef] = []
    for speaker in speakers:
        if speaker.rss is None:
            continue  # synthetic speaker (e.g., FOMC committee)
        parsed = feedparser.parse(speaker.rss)
        for entry in parsed.entries:
            if entry.get("category", "").lower() != "speech":
                continue
            url = entry.link
            pub = _parse_pub_date(entry.get("published", ""))
            if pub is None:
                continue
            if since and pub < since:
                continue
            refs.append(
                SpeechRef(
                    url=url,
                    speaker_key=speaker.key,
                    title=entry.title,
                    pub_date=pub,
                    source="frb_board",
                )
            )
    return refs


def fetch_speech(ref: SpeechRef, *, pw=None) -> Speech:
    """Download and parse a single speech page.

    Dispatches by `ref.source`. Regional banks delegate to fetchers/regional.py.
    """
    if ref.source.startswith("regional_"):
        from . import regional as regional_mod
        return regional_mod.fetch_regional(ref, pw=pw)

    resp = requests.get(
        ref.url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    # federalreserve.gov serves UTF-8 but doesn't declare it in Content-Type,
    # so requests falls back to ISO-8859-1 and mangles en-dashes etc.
    resp.encoding = "utf-8"
    return _parse_speech_html(resp.text, ref)


def _parse_speech_html(html: str, ref: SpeechRef) -> Speech:
    parsed = parse_article_html(html, source_url=ref.url, fallback_date=ref.pub_date,
                                fallback_title=ref.title)
    return Speech(
        url=ref.url,
        speaker_key=ref.speaker_key,
        speech_date=parsed["date"],
        title=parsed["title"],
        location=parsed["location"],
        body=parsed["body"],
    )


def parse_article_html(
    html: str,
    *,
    source_url: str,
    fallback_date: dt.date,
    fallback_title: str = "",
) -> dict:
    """Parse the `div#article` container shared by FRB speech, statement, and
    minutes pages. Returns a dict with `date`, `title`, `location`, `body`.

    The same DOM is used by:
      /newsevents/speech/<lastname><yyyymmdd><suffix>.htm   (speeches)
      /newsevents/pressreleases/monetary<yyyymmdd>a.htm    (FOMC statements/minutes)
    """
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("div", id="article")
    if article is None:
        raise ValueError(f"No #article container found at {source_url}")

    time_p = article.find("p", class_="article__time")
    location_p = article.find("p", class_="location")
    title_em = article.select_one("h3.title em")
    title_h3 = article.select_one("h3.title")

    parsed_date = (
        _parse_long_date(time_p.get_text(strip=True)) if time_p else fallback_date
    )
    location = location_p.get_text(strip=True) if location_p else ""
    # Speeches use <h3 class='title'><em>...</em></h3>; statements use plain <h3 class='title'>.
    if title_em:
        title = title_em.get_text(strip=True)
    elif title_h3:
        title = title_h3.get_text(strip=True)
    else:
        title = fallback_title

    # Body lives in a sibling col-xs-12 col-sm-8 col-md-8 div that follows the heading div.
    # The heading div ALSO has class "col-xs-12 col-sm-8 col-md-8" (with "heading" prefix).
    body_divs = article.select("div.col-xs-12.col-sm-8.col-md-8")
    body_div = next((d for d in body_divs if "heading" not in d.get("class", [])), None)
    if body_div is None:
        raise ValueError(f"No body div found at {source_url}")

    body = _extract_body_text(body_div)
    if not body.strip():
        raise ValueError(f"Empty body extracted from {source_url}")

    return {"date": parsed_date, "title": title, "location": location, "body": body}


def _extract_body_text(body_div) -> str:
    """Extract paragraph text from an FRB speech body, including the
    page-specific boilerplate (sr-only video help, videoDetails host div,
    list-unstyled video keyboard hints) that lives inside the same container."""
    for sr in body_div.find_all("div", class_="sr-only"):
        sr.decompose()
    for vd in body_div.find_all("div", id=lambda v: v and v.startswith("videoDetails")):
        vd.decompose()
    for ul in body_div.find_all("ul", class_="list-unstyled"):
        ul.decompose()
    return extract_paragraphs(body_div)


def extract_paragraphs(node) -> str:
    """Extract paragraph text from a BeautifulSoup node.

    Strips `<script>`, `<style>`, and footnote `<sup>` markers so digits
    don't bleed into prose, then joins all remaining `<p>` text with `\\n\\n`.
    Shared by FRB and regional bank parsers.
    """
    for tag in node.find_all(["script", "style"]):
        tag.decompose()
    for sup in node.find_all("sup"):
        sup.decompose()

    paragraphs: list[str] = []
    for p in node.find_all("p"):
        text = p.get_text(" ", strip=True)
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


_MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def _parse_long_date(s: str) -> dt.date:
    """Parse 'May 05, 2026' / 'May 5, 2026' style dates."""
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})$", s.strip())
    if not m:
        raise ValueError(f"Cannot parse date: {s!r}")
    month_name, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    month = _MONTHS.get(month_name.capitalize())
    if month is None:
        raise ValueError(f"Unknown month: {month_name!r}")
    return dt.date(year, month, day)


def _parse_pub_date(s: str) -> dt.date | None:
    """Parse RFC822 pubDate from RSS, returning the date portion."""
    if not s:
        return None
    try:
        # feedparser keeps the original string; let email.utils handle it.
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None
