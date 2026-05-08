"""Discover and fetch FOMC documents (statements, minutes, press conferences).

Discovery uses the consolidated `feeds/press_monetary.xml` for statements
and minutes. Press-conference transcripts are not in any feed; their URLs
follow a fixed pattern keyed off the meeting date, so we derive them from
each statement and probe with a HEAD request.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

import feedparser
import requests
from bs4 import BeautifulSoup

from .federalreserve import (
    REQUEST_TIMEOUT,
    USER_AGENT,
    parse_article_html,
    _parse_pub_date,
)
from .pdf import fetch_pdf_text

PRESS_MONETARY_RSS = "https://www.federalreserve.gov/feeds/press_monetary.xml"
PRESSER_URL_TEMPLATE = (
    "https://www.federalreserve.gov/mediacenter/files/FOMCpresconf{ymd}.pdf"
)

# doc_type values used in the speeches table
DOC_STATEMENT = "fomc_statement"
DOC_MINUTES = "fomc_minutes"
DOC_PRESSER = "fomc_presser"

# Speaker keys used for the synthetic 'fomc' speaker and Powell pressers.
FOMC_SPEAKER_KEY = "fomc"
POWELL_SPEAKER_KEY = "powell"


@dataclass
class FomcRef:
    """Pointer to an FOMC document discovered via RSS or URL derivation."""
    url: str
    doc_type: str
    speaker_key: str
    pub_date: dt.date
    title: str  # only meaningful for HTML docs; pressers get a synthesized title


@dataclass
class FomcDoc:
    """Fully-fetched FOMC document, ready for scoring/storage."""
    url: str
    doc_type: str
    speaker_key: str
    speech_date: dt.date
    title: str
    body: str


# ---------------------------------------------------------------------- discover


def discover(since: dt.date | None = None) -> list[FomcRef]:
    """Pull statements + minutes from the press_monetary feed and derive
    matching press-conference URLs by probing the predictable PDF path."""
    parsed = feedparser.parse(PRESS_MONETARY_RSS)
    refs: list[FomcRef] = []

    for entry in parsed.entries:
        title = entry.get("title", "").strip()
        url = entry.get("link", "").strip()
        pub = _parse_pub_date(entry.get("published", ""))
        if not (title and url and pub):
            continue
        if since and pub < since:
            continue

        doc_type = _classify(title)
        if doc_type is None:
            continue
        refs.append(
            FomcRef(
                url=url,
                doc_type=doc_type,
                speaker_key=FOMC_SPEAKER_KEY,
                pub_date=pub,
                title=title,
            )
        )

    # For each statement, probe the press-conf PDF URL. Some FOMC meetings
    # don't have a press conference (rare unscheduled meetings, statements
    # released for non-decision events) — those return 404 and we skip.
    statement_refs = [r for r in refs if r.doc_type == DOC_STATEMENT]
    for s in statement_refs:
        meeting_ymd = _meeting_ymd_from_url(s.url)
        if meeting_ymd is None:
            continue
        presser_url = PRESSER_URL_TEMPLATE.format(ymd=meeting_ymd)
        if not _head_ok(presser_url):
            continue
        meeting_date = dt.date(
            int(meeting_ymd[0:4]), int(meeting_ymd[4:6]), int(meeting_ymd[6:8])
        )
        if since and meeting_date < since:
            continue
        refs.append(
            FomcRef(
                url=presser_url,
                doc_type=DOC_PRESSER,
                speaker_key=POWELL_SPEAKER_KEY,
                pub_date=meeting_date,
                title=f"Press Conference Transcript ({meeting_date.isoformat()})",
            )
        )

    return refs


# ----------------------------------------------------------------------- fetch


def fetch_doc(ref: FomcRef) -> FomcDoc:
    """Download and parse one FOMC document."""
    if ref.doc_type == DOC_PRESSER:
        body = fetch_pdf_text(ref.url, drop_first_page=True)
        if not body.strip():
            raise ValueError(f"Empty press-conf transcript: {ref.url}")
        return FomcDoc(
            url=ref.url,
            doc_type=ref.doc_type,
            speaker_key=ref.speaker_key,
            speech_date=ref.pub_date,
            title=ref.title,
            body=body,
        )

    if ref.doc_type == DOC_MINUTES:
        # The press-release page at the feed URL is just an announcement that
        # links to the actual minutes at /monetarypolicy/fomcminutes<...>.htm.
        # That page uses a different (simpler) DOM, so we parse it specially.
        announcement = _http_get(ref.url)
        minutes_url = _find_minutes_link(announcement, ref.url)
        if minutes_url is None:
            raise ValueError(f"Cannot find minutes link in announcement: {ref.url}")
        minutes_html = _http_get(minutes_url)
        body = _parse_minutes_body(minutes_html, source_url=minutes_url)
        return FomcDoc(
            url=ref.url,         # canonical = the announcement URL (RSS-stable)
            doc_type=ref.doc_type,
            speaker_key=ref.speaker_key,
            speech_date=ref.pub_date,
            title=ref.title,
            body=body,
        )

    # Statements: the announcement page IS the statement; reuse speech parser
    statement_html = _http_get(ref.url)
    parsed = parse_article_html(
        statement_html,
        source_url=ref.url,
        fallback_date=ref.pub_date,
        fallback_title=ref.title,
    )
    return FomcDoc(
        url=ref.url,
        doc_type=ref.doc_type,
        speaker_key=ref.speaker_key,
        speech_date=parsed["date"],
        title=parsed["title"],
        body=parsed["body"],
    )


# ---------------------------------------------------------------------- helpers


_STATEMENT_TITLE = re.compile(r"^Federal Reserve issues FOMC statement\s*$", re.I)
_MINUTES_TITLE = re.compile(r"^Minutes of the Federal Open Market Committee", re.I)


def _classify(title: str) -> str | None:
    """Return DOC_STATEMENT / DOC_MINUTES, or None to skip the entry.

    The press_monetary feed also contains discount-rate-meeting minutes and
    economic-projections announcements; we explicitly skip those.
    """
    if _STATEMENT_TITLE.match(title):
        return DOC_STATEMENT
    if _MINUTES_TITLE.match(title):
        return DOC_MINUTES
    return None


_URL_DATE_RE = re.compile(r"/monetary(\d{8})[a-z]\.htm$", re.I)


def _meeting_ymd_from_url(url: str) -> str | None:
    """Extract YYYYMMDD from a /monetary<yyyymmdd>a.htm URL."""
    m = _URL_DATE_RE.search(url)
    return m.group(1) if m else None


_MINUTES_HREF_RE = re.compile(
    r'href="([^"]*?/monetarypolicy/fomcminutes\d{8}\.htm)"', re.I
)


def _http_get(url: str) -> str:
    resp = requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    # federalreserve.gov serves UTF-8 but doesn't declare a charset,
    # so requests falls back to ISO-8859-1 and mangles en-dashes etc.
    resp.encoding = "utf-8"
    return resp.text


def _find_minutes_link(announcement_html: str, announcement_url: str) -> str | None:
    """Find the /monetarypolicy/fomcminutes<...>.htm link inside the announcement."""
    m = _MINUTES_HREF_RE.search(announcement_html)
    if not m:
        return None
    href = m.group(1)
    if href.startswith("http"):
        return href
    # Relative path
    return f"https://www.federalreserve.gov{href}"


def _parse_minutes_body(html: str, *, source_url: str) -> str:
    """The actual minutes page uses a simpler DOM: `div#article` directly
    contains `<h3>` + `<p>` paragraphs (no heading sub-div). Extract every
    paragraph after the title."""
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("div", id="article")
    if article is None:
        raise ValueError(f"No #article container on minutes page: {source_url}")

    # Drop footnote superscripts.
    for sup in article.find_all("sup"):
        sup.decompose()

    paragraphs: list[str] = []
    for p in article.find_all("p"):
        text = p.get_text(" ", strip=True)
        if text:
            paragraphs.append(text)
    body = "\n\n".join(paragraphs)
    if len(body) < 1000:
        # Real FOMC minutes are 5-15K words. Anything tiny means we missed.
        raise ValueError(
            f"Minutes body suspiciously short ({len(body)} chars) at {source_url}"
        )
    return body


def _head_ok(url: str) -> bool:
    try:
        resp = requests.head(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        return resp.status_code == 200
    except requests.RequestException:
        return False


def doc_type_label(doc_type: str) -> str:
    """Human-friendly label for dashboards and email subject lines."""
    return {
        DOC_STATEMENT: "FOMC Statement",
        DOC_MINUTES: "FOMC Minutes",
        DOC_PRESSER: "Press Conference",
    }.get(doc_type, doc_type)
