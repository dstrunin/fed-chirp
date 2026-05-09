"""Discover and fetch speeches from the 12 regional Federal Reserve bank presidents.

Three parser families dispatched by `speaker.key` via the PARSERS registry:
  - RssHtmlParser   (Group A): RSS feed + server-rendered HTML body
  - IndexHtmlParser (Group B): scrape listing page + server-rendered HTML body
  - JsIndexParser   (Group C): same as B but via headless Chromium (Playwright)

Each parser subclasses one of the three bases and overrides only its CSS
selectors / per-bank quirks. Discovery and fetch entry points are
`discover_regional()` and `fetch_regional()`, called from federalreserve.py
once it has partitioned speakers by `source`.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

from .federalreserve import (
    REQUEST_TIMEOUT,
    USER_AGENT,
    Speaker,
    Speech,
    SpeechRef,
    extract_paragraphs,
)
from .pdf import fetch_pdf_text


# ---- shared helpers ---------------------------------------------------------


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

_LONG_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)


def _http_get(url: str) -> str:
    """GET a URL with project User-Agent. Forces UTF-8 to avoid mojibake on
    Fed sites that don't declare encoding in Content-Type."""
    resp = requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def _parse_long_date_text(s: str) -> dt.date | None:
    """Find the first 'March 27, 2026' / 'May 5 2026' style date in `s`."""
    m = _LONG_DATE_RE.search(s)
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if month is None:
        return None
    return dt.date(int(m.group(3)), month, int(m.group(2)))


def _parse_iso_date(s: str) -> dt.date | None:
    """Parse '2026-04-15' or '2026-04-15T10:07:27-07:00' style ISO dates."""
    if not s:
        return None
    s = s.strip()
    try:
        if "T" in s:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _extract_meta_date(soup: BeautifulSoup) -> dt.date | None:
    """Try common meta-tag locations for a publication date.

    Order: <meta property="article:published_time">, <meta name="releaseDate">,
    <meta name="citation_date">, JSON-LD datePublished, then a long-date
    regex over <meta property="og:description">.
    """
    selectors = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "releaseDate"}),
        ("meta", {"name": "citation_date"}),
        ("meta", {"name": "publication_date"}),
        ("meta", {"property": "og:article:published_time"}),
        ("meta", {"name": "ess:publishdatetime"}),  # Minneapolis Fed
        ("meta", {"name": "sortDate"}),              # Minneapolis Fed, Cleveland Fed
    ]
    for name, attrs in selectors:
        tag = soup.find(name, attrs=attrs)
        if tag and tag.get("content"):
            d = _parse_iso_date(tag["content"])
            if d:
                return d

    for ld in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(ld.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            dp = item.get("datePublished") or item.get("dateCreated")
            if dp:
                d = _parse_iso_date(dp)
                if d:
                    return d

    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content"):
        d = _parse_long_date_text(og["content"])
        if d:
            return d
    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        d = _parse_long_date_text(desc["content"])
        if d:
            return d
    return None


def _extract_title(soup: BeautifulSoup) -> str:
    """Best-effort speech title: page <h1>, then og:title, then <title>."""
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    t = soup.find("title")
    return t.get_text(strip=True) if t else ""


# ---- Playwright lifecycle ---------------------------------------------------


class PlaywrightContext:
    """Lazy Playwright wrapper. Group C parsers receive an instance via `pw=`
    and call `pw.html(url)` to get rendered DOM. Single browser, single page,
    reused across all fetches in one CLI invocation."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self) -> "PlaywrightContext":
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        # Prefer system Chrome over bundled chromium-headless-shell — some
        # Fed CDN configs (notably St. Louis) reject the headless-shell user
        # agent / TLS fingerprint with HTTP/2 protocol errors.
        try:
            self._browser = self._pw.chromium.launch(
                channel="chrome", headless=True
            )
        except Exception:
            self._browser = self._pw.chromium.launch(headless=True)
        ctx = self._browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        self._page = ctx.new_page()
        self._page.set_default_navigation_timeout(30_000)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    def html(self, url: str, *, wait: str = "load", settle_ms: int = 4000) -> str:
        """Fetch a URL and return the rendered HTML.

        We don't use `networkidle` because Fed bank sites have constant
        background traffic (analytics beacons, lazy chat widgets) that
        prevents networkidle from ever firing. Instead we wait for `load`
        (DOM + main resources done) plus a brief settle for client-side
        list rendering to populate.
        """
        assert self._page is not None, "PlaywrightContext not entered"
        self._page.goto(url, wait_until=wait, timeout=30_000)
        if settle_ms:
            self._page.wait_for_timeout(settle_ms)
        return self._page.content()


@contextlib.contextmanager
def maybe_playwright(speakers: list[Speaker]):
    """Yield a PlaywrightContext only if any speaker needs JS rendering AND
    has a registered parser. Otherwise yields None — Group A/B parsers simply
    ignore the `pw` arg, and JS speakers without a parser yet are skipped
    silently in discover_regional."""
    needs_js = any(
        s.source == "regional_js" and s.key in PARSERS for s in speakers
    )
    if needs_js:
        with PlaywrightContext() as pw:
            yield pw
    else:
        yield None


# ---- parser protocol + base classes -----------------------------------------


class BankParser(Protocol):
    def discover(
        self, speaker: Speaker, since: dt.date | None, *, pw=None
    ) -> list[SpeechRef]: ...

    def fetch(self, ref: SpeechRef, *, pw=None) -> Speech: ...


@dataclass
class _BodyParse:
    date: dt.date
    title: str
    body: str


class RssHtmlParser:
    """Group A base: discover via RSS, fetch via plain HTTP + per-bank selectors.

    Subclasses override:
      BODY_SELECTOR: CSS selector for the speech body wrapper.
      DOMAIN_ALLOWLIST: hostnames to keep (RSS often includes YouTube/event
                       links — we can only score text on the bank's own site).
      _parse_body(soup, ref) -> _BodyParse: per-bank date/title/body extraction.
    """

    BODY_SELECTOR: str = ""
    DOMAIN_ALLOWLIST: tuple[str, ...] = ()

    def discover(
        self, speaker: Speaker, since: dt.date | None, *, pw=None
    ) -> list[SpeechRef]:
        if speaker.rss is None:
            return []
        parsed = feedparser.parse(speaker.rss)
        refs: list[SpeechRef] = []
        for entry in parsed.entries:
            url = entry.get("link", "").strip()
            if not url:
                continue
            if self.DOMAIN_ALLOWLIST and not any(
                host in url for host in self.DOMAIN_ALLOWLIST
            ):
                continue
            pub = _parse_rfc822(entry.get("published", ""))
            if pub is None:
                pub = _parse_rfc822(entry.get("updated", ""))
            if pub is None:
                continue
            if since and pub < since:
                continue
            title = (entry.get("title") or "").strip()
            refs.append(
                SpeechRef(
                    url=url,
                    speaker_key=speaker.key,
                    title=title,
                    pub_date=pub,
                    source="regional_rss_html",
                )
            )
        return refs

    def fetch(self, ref: SpeechRef, *, pw=None) -> Speech:
        html = _http_get(ref.url)
        soup = BeautifulSoup(html, "lxml")
        parsed = self._parse_body(soup, ref)
        if not parsed.body.strip():
            raise ValueError(f"Empty body extracted from {ref.url}")
        return Speech(
            url=ref.url,
            speaker_key=ref.speaker_key,
            speech_date=parsed.date,
            title=parsed.title or ref.title,
            location="",
            body=parsed.body,
        )

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        node = soup.select_one(self.BODY_SELECTOR)
        if node is None:
            raise ValueError(
                f"No '{self.BODY_SELECTOR}' wrapper at {ref.url}"
            )
        return _BodyParse(
            date=_extract_meta_date(soup) or ref.pub_date,
            title=_extract_title(soup) or ref.title,
            body=extract_paragraphs(node),
        )


def _parse_rfc822(s: str) -> dt.date | None:
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


# ---- Group A parsers (RSS + HTML) -------------------------------------------


class RichmondParser(RssHtmlParser):
    """https://www.richmondfed.org/press_room/speeches/thomas_i_barkin/<yyyy>/...

    The bank's RSS feed mixes Barkin speeches with research-director Athreya
    speeches and other content; filter to the President's path slug.
    """
    BODY_SELECTOR = "div.tmplt.speech"
    DOMAIN_ALLOWLIST = ("/press_room/speeches/thomas_i_barkin/",)

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        node = soup.select_one(self.BODY_SELECTOR)
        if node is None:
            raise ValueError(f"No 'div.tmplt.speech' at {ref.url}")
        date_div = node.select_one("div.speech-mast-head__date")
        date = (
            _parse_long_date_text(date_div.get_text(" ", strip=True))
            if date_div else None
        ) or _extract_meta_date(soup) or ref.pub_date
        return _BodyParse(
            date=date,
            title=_extract_title(soup) or ref.title,
            body=extract_paragraphs(node),
        )


class AtlantaParser(RssHtmlParser):
    """https://www.atlantafed.org/news-and-events/speeches/<yyyy>/<mm>/<dd>/<slug>

    The Atlanta speech RSS interleaves YouTube live links and external news
    appearances; only speeches on atlantafed.org have transcripts to score.
    """
    BODY_SELECTOR = "article.article-frame"
    DOMAIN_ALLOWLIST = ("atlantafed.org",)

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        node = soup.select_one(self.BODY_SELECTOR)
        if node is None:
            raise ValueError(f"No 'article.article-frame' at {ref.url}")
        date_p = node.select_one("p.article-date")
        date = (
            _parse_long_date_text(date_p.get_text(" ", strip=True))
            if date_p else None
        ) or _extract_meta_date(soup) or ref.pub_date
        return _BodyParse(
            date=date,
            title=_extract_title(soup) or ref.title,
            body=extract_paragraphs(node),
        )


# ---- Group B parsers (HTML index, no RSS) -----------------------------------


class IndexHtmlParser:
    """Group B base: discover via scraping the speeches index page,
    fetch via plain HTTP + per-bank selectors.

    Subclasses override:
      INDEX_RENDERED_BY_JS: True for Group C (handled by JsIndexParser).
      BODY_SELECTOR: CSS selector for the speech body wrapper.
      _parse_index(soup, speaker, since) -> list[SpeechRef]
      _parse_body(soup, ref) -> _BodyParse
    """

    INDEX_RENDERED_BY_JS: bool = False
    BODY_SELECTOR: str = ""

    def discover(
        self, speaker: Speaker, since: dt.date | None, *, pw=None
    ) -> list[SpeechRef]:
        if speaker.index_url is None:
            return []
        html = self._get_html(speaker.index_url, pw=pw)
        soup = BeautifulSoup(html, "lxml")
        return self._parse_index(soup, speaker, since)

    def fetch(self, ref: SpeechRef, *, pw=None) -> Speech:
        html = self._get_html(ref.url, pw=pw)
        soup = BeautifulSoup(html, "lxml")
        parsed = self._parse_body(soup, ref)
        if not parsed.body.strip():
            raise ValueError(f"Empty body extracted from {ref.url}")
        return Speech(
            url=ref.url,
            speaker_key=ref.speaker_key,
            speech_date=parsed.date,
            title=parsed.title or ref.title,
            location="",
            body=parsed.body,
        )

    def _get_html(self, url: str, *, pw=None) -> str:
        if self.INDEX_RENDERED_BY_JS and pw is not None:
            return pw.html(url)
        return _http_get(url)

    def _parse_index(
        self, soup: BeautifulSoup, speaker: Speaker, since: dt.date | None
    ) -> list[SpeechRef]:
        raise NotImplementedError

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        node = soup.select_one(self.BODY_SELECTOR)
        if node is None:
            raise ValueError(
                f"No '{self.BODY_SELECTOR}' wrapper at {ref.url}"
            )
        return _BodyParse(
            date=_extract_meta_date(soup) or ref.pub_date,
            title=_extract_title(soup) or ref.title,
            body=extract_paragraphs(node),
        )


class BostonParser(IndexHtmlParser):
    """https://www.bostonfed.org/news-and-events/speeches.aspx — card listing.
    Speech detail pages on bostonfed.org are stubs that link to a PDF
    transcript at /-/media/Documents/Speeches/PDF/...; pull text from the PDF.
    """

    def _parse_index(
        self, soup: BeautifulSoup, speaker: Speaker, since: dt.date | None
    ) -> list[SpeechRef]:
        refs: list[SpeechRef] = []
        for card in soup.select("h1.card-title"):
            a = card.find("a", href=True)
            if a is None:
                continue
            href = a["href"]
            if not href.startswith("/news-and-events/speeches/"):
                continue
            url = urljoin("https://www.bostonfed.org", href)
            title = a.get_text(" ", strip=True)
            date_p = None
            row = card.find_parent("div", class_="row")
            if row is not None:
                date_p = row.select_one("p.date-and-location")
            pub = (
                _parse_long_date_text(date_p.get_text(" ", strip=True))
                if date_p else None
            )
            if pub is None:
                continue
            if since and pub < since:
                continue
            refs.append(SpeechRef(
                url=url, speaker_key=speaker.key, title=title,
                pub_date=pub, source="regional_html",
            ))
        return refs

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        # Find the PDF transcript link on the speech stub page.
        pdf_url: str | None = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().endswith(".pdf") and "speeches/pdf" in href.lower():
                pdf_url = urljoin("https://www.bostonfed.org", href)
                break
        if pdf_url is None:
            raise ValueError(f"No speech PDF link found at {ref.url}")
        body = fetch_pdf_text(pdf_url, drop_first_page=False)
        return _BodyParse(
            date=_extract_meta_date(soup) or ref.pub_date,
            title=_extract_title(soup) or ref.title,
            body=body,
        )


class NewYorkParser(IndexHtmlParser):
    """https://www.newyorkfed.org/newsevents/speeches/index — table of all
    bank speakers; filter to John Williams' speeches by `wil` URL prefix.

    Slug format: /newsevents/speeches/<yyyy>/<3-letter-author><yymmdd>
    """

    BODY_SELECTOR = "div.ts-article-text"
    AUTHOR_SLUG = "wil"

    def _parse_index(
        self, soup: BeautifulSoup, speaker: Speaker, since: dt.date | None
    ) -> list[SpeechRef]:
        refs: list[SpeechRef] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.match(
                r"/newsevents/speeches/(\d{4})/([a-z]{3})(\d{6})\b",
                href,
            )
            if not m:
                continue
            if m.group(2) != self.AUTHOR_SLUG:
                continue
            year, ymd = m.group(1), m.group(3)
            try:
                pub = dt.date(
                    2000 + int(ymd[:2]),
                    int(ymd[2:4]),
                    int(ymd[4:6]),
                )
            except ValueError:
                continue
            if since and pub < since:
                continue
            url = urljoin("https://www.newyorkfed.org", href)
            if url in seen:
                continue
            seen.add(url)
            title = a.get_text(" ", strip=True)
            refs.append(SpeechRef(
                url=url, speaker_key=speaker.key, title=title,
                pub_date=pub, source="regional_html",
            ))
        return refs


class SanFranciscoParser(IndexHtmlParser):
    """https://www.frbsf.org/news-and-media/speeches/ — FacetWP-driven WordPress
    listing with date as a sibling div; speech body in div.entry-content."""

    BODY_SELECTOR = "div.entry-content"

    def _parse_index(
        self, soup: BeautifulSoup, speaker: Speaker, since: dt.date | None
    ) -> list[SpeechRef]:
        refs: list[SpeechRef] = []
        seen: set[str] = set()
        for result in soup.select("div.fwpl-result"):
            a = result.find("a", href=True)
            if a is None:
                continue
            href = a["href"]
            if "/speeches/mary-c-daly/" not in href:
                continue
            title = a.get_text(" ", strip=True)
            # Date lives in a sibling div without a stable class — find any
            # text within the result that parses as a long date.
            pub: dt.date | None = None
            for div in result.find_all(["div", "span"]):
                d = _parse_long_date_text(div.get_text(" ", strip=True))
                if d:
                    pub = d
                    break
            if pub is None:
                continue
            if since and pub < since:
                continue
            if href in seen:
                continue
            seen.add(href)
            refs.append(SpeechRef(
                url=href, speaker_key=speaker.key, title=title,
                pub_date=pub, source="regional_html",
            ))
        return refs


# ---- Group C parsers (Playwright) -------------------------------------------


class JsIndexParser(IndexHtmlParser):
    """Group C base: index page is rendered client-side; pull it through
    Playwright. Speech body pages are sometimes server-rendered (try requests
    first, fall back to Playwright if BODY_NEEDS_JS or empty)."""

    INDEX_RENDERED_BY_JS = True
    BODY_NEEDS_JS: bool = False

    def fetch(self, ref: SpeechRef, *, pw=None) -> Speech:
        # Try plain HTTP first; fall back to Playwright if the body wrapper
        # is missing (page is JS-rendered) or per-bank flag forces it.
        if not self.BODY_NEEDS_JS:
            try:
                html = _http_get(ref.url)
                soup = BeautifulSoup(html, "lxml")
                if soup.select_one(self.BODY_SELECTOR) is not None:
                    parsed = self._parse_body(soup, ref)
                    if parsed.body.strip():
                        return Speech(
                            url=ref.url, speaker_key=ref.speaker_key,
                            speech_date=parsed.date,
                            title=parsed.title or ref.title,
                            location="", body=parsed.body,
                        )
            except (requests.RequestException, requests.HTTPError):
                pass
        if pw is None:
            raise ValueError(
                f"Body fetch needs Playwright but pw is None: {ref.url}"
            )
        html = pw.html(ref.url, wait="domcontentloaded")
        soup = BeautifulSoup(html, "lxml")
        parsed = self._parse_body(soup, ref)
        if not parsed.body.strip():
            raise ValueError(f"Empty body extracted from {ref.url}")
        return Speech(
            url=ref.url, speaker_key=ref.speaker_key,
            speech_date=parsed.date, title=parsed.title or ref.title,
            location="", body=parsed.body,
        )


def _largest_paragraph_node(soup: BeautifulSoup, selector: str):
    """When a CSS selector matches multiple containers and the speech body is
    in whichever has the most paragraphs, pick that one."""
    best = None
    best_n = 0
    for n in soup.select(selector):
        n_p = len(n.find_all("p"))
        if n_p > best_n:
            best, best_n = n, n_p
    return best


class _LargestPSelector(JsIndexParser):
    """JS parser variant where BODY_SELECTOR matches multiple nodes and we
    want the one with the most <p> tags."""

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        node = _largest_paragraph_node(soup, self.BODY_SELECTOR)
        if node is None:
            raise ValueError(f"No '{self.BODY_SELECTOR}' wrapper at {ref.url}")
        return _BodyParse(
            date=_extract_meta_date(soup) or ref.pub_date,
            title=_extract_title(soup) or ref.title,
            body=extract_paragraphs(node),
        )


class ChicagoParser(_LargestPSelector):
    """Chicago renders both index and detail via JS; speech body lives in
    one of several `div.cfedContent__text` blocks (the largest is the body).
    """
    BODY_SELECTOR = "div.cfedContent__text"
    BODY_NEEDS_JS = True

    def _parse_index(
        self, soup: BeautifulSoup, speaker: Speaker, since: dt.date | None
    ) -> list[SpeechRef]:
        refs: list[SpeechRef] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/publications/speeches/(\d{4})/([^?#\s\"']+)", href)
            if not m:
                continue
            year, slug = int(m.group(1)), m.group(2)
            # Slug often starts with "<month-name>-<day>-..." so we can pull
            # the date out of the URL itself.
            pub = dt.date(year, 1, 1)
            sm = re.match(
                r"^(january|february|march|april|may|june|july|august|"
                r"september|october|november|december)-(\d{1,2})-",
                slug, re.IGNORECASE,
            )
            if sm:
                month = _MONTHS.get(sm.group(1).lower())
                day = int(sm.group(2))
                if month:
                    try:
                        pub = dt.date(year, month, day)
                    except ValueError:
                        pass
            if since and pub < since:
                continue
            url = urljoin("https://www.chicagofed.org", href)
            if url in seen:
                continue
            seen.add(url)
            title = a.get_text(" ", strip=True) or slug
            refs.append(SpeechRef(
                url=url, speaker_key=speaker.key, title=title,
                pub_date=pub, source="regional_js",
            ))
        return refs


class ClevelandParser(JsIndexParser):
    """Cleveland: speech listing on /collections/speeches (JS-rendered),
    detail pages have releaseDate meta + content under div.component-content.
    """
    BODY_SELECTOR = "div.component-content"

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        # Pick the component-content div with the most paragraphs.
        node = _largest_paragraph_node(soup, "div.component-content")
        if node is None:
            raise ValueError(f"No 'div.component-content' at {ref.url}")
        return _BodyParse(
            date=_extract_meta_date(soup) or ref.pub_date,
            title=_extract_title(soup) or ref.title,
            body=extract_paragraphs(node),
        )

    def _parse_index(
        self, soup: BeautifulSoup, speaker: Speaker, since: dt.date | None
    ) -> list[SpeechRef]:
        # Cleveland exposes both /collections/speeches/<yyyy>/sp-... and
        # /collections/speeches/sp-... for the same article; dedupe by slug
        # and prefer the year-prefixed canonical version.
        by_slug: dict[str, SpeechRef] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.match(
                r"/collections/speeches/(?:(\d{4})/)?sp-(\d{8})-([^?#\s\"']+)",
                href,
            )
            if not m:
                continue
            year_group, ymd, slug = m.group(1), m.group(2), m.group(3)
            try:
                pub = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
            except ValueError:
                continue
            if since and pub < since:
                continue
            url = urljoin("https://www.clevelandfed.org", href)
            title = a.get_text(" ", strip=True) or slug
            ref = SpeechRef(
                url=url, speaker_key=speaker.key, title=title,
                pub_date=pub, source="regional_js",
            )
            existing = by_slug.get(slug)
            # Prefer the year-prefixed URL (canonical) over the bare one.
            if existing is None or (year_group and "/sp-" in existing.url
                                    and f"/{year_group}/sp-" not in existing.url):
                by_slug[slug] = ref
        return list(by_slug.values())


class StLouisParser(JsIndexParser):
    """St. Louis blocks plain `requests` (HTTP/2 quirk) so even body pages
    require Playwright. Date lives in og:description metadata."""
    BODY_SELECTOR = "div.component.content"
    BODY_NEEDS_JS = True

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        node = _largest_paragraph_node(soup, "div.component.content")
        if node is None:
            raise ValueError(f"No 'div.component.content' at {ref.url}")
        return _BodyParse(
            date=_extract_meta_date(soup) or ref.pub_date,
            title=_extract_title(soup) or ref.title,
            body=extract_paragraphs(node),
        )

    def _parse_index(
        self, soup: BeautifulSoup, speaker: Speaker, since: dt.date | None
    ) -> list[SpeechRef]:
        refs: list[SpeechRef] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.match(
                r"/from-the-president/remarks/(\d{4})/([^?#\s\"']+)",
                href,
            )
            if not m:
                continue
            year = int(m.group(1))
            if since and year < since.year:
                continue
            url = urljoin("https://www.stlouisfed.org", href)
            if url in seen:
                continue
            seen.add(url)
            title = a.get_text(" ", strip=True) or m.group(2)
            # Year-only fallback; real date pulled from page meta.
            refs.append(SpeechRef(
                url=url, speaker_key=speaker.key, title=title,
                pub_date=dt.date(year, 1, 1), source="regional_js",
            ))
        return refs


class DallasParser(JsIndexParser):
    """Dallas: /news/speeches/logan/<yyyy>/lkl<yymmdd>. Site uses a custom
    'dal-' CSS namespace; speech body lives in div.dal-main-content."""
    BODY_SELECTOR = "div.dal-main-content"

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        node = _largest_paragraph_node(soup, self.BODY_SELECTOR)
        if node is None:
            raise ValueError(f"No 'div.dal-main-content' at {ref.url}")
        return _BodyParse(
            date=_extract_meta_date(soup) or ref.pub_date,
            title=_extract_title(soup) or ref.title,
            body=extract_paragraphs(node),
        )

    def _parse_index(
        self, soup: BeautifulSoup, speaker: Speaker, since: dt.date | None
    ) -> list[SpeechRef]:
        refs: list[SpeechRef] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.match(
                r"/news/speeches/logan/(\d{4})/lkl(\d{6})\b",
                href,
            )
            if not m:
                continue
            ymd = m.group(2)
            try:
                pub = dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
            except ValueError:
                continue
            if since and pub < since:
                continue
            url = urljoin("https://www.dallasfed.org", href)
            if url in seen:
                continue
            seen.add(url)
            title = a.get_text(" ", strip=True)
            refs.append(SpeechRef(
                url=url, speaker_key=speaker.key, title=title,
                pub_date=pub, source="regional_js",
            ))
        return refs


class KansasCityParser(JsIndexParser):
    """KC: /speeches/<slug>/. Speech listing renders client-side."""
    BODY_SELECTOR = "div.field--name-body"

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        node = _largest_paragraph_node(soup, self.BODY_SELECTOR)
        if node is None:
            # Fall back to article element
            node = soup.find("article")
        if node is None:
            raise ValueError(f"No body wrapper at {ref.url}")
        return _BodyParse(
            date=_extract_meta_date(soup) or ref.pub_date,
            title=_extract_title(soup) or ref.title,
            body=extract_paragraphs(node),
        )

    def _parse_index(
        self, soup: BeautifulSoup, speaker: Speaker, since: dt.date | None
    ) -> list[SpeechRef]:
        refs: list[SpeechRef] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not re.match(r"^/speeches/[^/?#]+/?$", href):
                continue
            url = urljoin("https://www.kansascityfed.org", href)
            if url in seen:
                continue
            seen.add(url)
            title = a.get_text(" ", strip=True)
            # No date in URL — date lives only on the speech page itself.
            # Use today() as placeholder so it passes any `since` filter; the
            # real date is parsed in fetch via meta tags. The since filter
            # then re-applies after fetch (caller skips early-dated docs).
            refs.append(SpeechRef(
                url=url, speaker_key=speaker.key, title=title,
                pub_date=dt.date.today(), source="regional_js",
            ))
        return refs


class PhiladelphiaParser(JsIndexParser):
    """Philly: speeches mixed into /the-economy/monetary-policy/<slug>;
    not all entries under this path are Paulson speeches, so we filter
    by year fragment in the slug ('-2026', '-2025') as a coarse heuristic."""
    BODY_SELECTOR = "div.content-main"

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        node = _largest_paragraph_node(soup, self.BODY_SELECTOR) or \
               _largest_paragraph_node(soup, "div.component.content") or \
               soup.find("main")
        if node is None:
            raise ValueError(f"No body wrapper at {ref.url}")
        return _BodyParse(
            date=_extract_meta_date(soup) or ref.pub_date,
            title=_extract_title(soup) or ref.title,
            body=extract_paragraphs(node),
        )

    def _parse_index(
        self, soup: BeautifulSoup, speaker: Speaker, since: dt.date | None
    ) -> list[SpeechRef]:
        refs: list[SpeechRef] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.match(
                r"/the-economy/monetary-policy/([^?#\s\"']+)",
                href,
            )
            if not m:
                continue
            slug = m.group(1)
            # Most Philly speech slugs start with yymmdd, e.g.
            # "260306-2026-us-monetary-policy-forum". Fall back to a
            # year fragment if the date prefix is missing.
            pub: dt.date | None = None
            ym = re.match(r"^(\d{2})(\d{2})(\d{2})-", slug)
            if ym:
                try:
                    pub = dt.date(
                        2000 + int(ym.group(1)),
                        int(ym.group(2)),
                        int(ym.group(3)),
                    )
                except ValueError:
                    pub = None
            if pub is None:
                yy = re.search(r"\b(20\d{2})\b", slug)
                if yy:
                    pub = dt.date(int(yy.group(1)), 1, 1)
            if pub is None:
                continue
            if since and pub < since:
                continue
            url = urljoin("https://www.philadelphiafed.org", href)
            if url in seen:
                continue
            seen.add(url)
            title = a.get_text(" ", strip=True) or slug
            refs.append(SpeechRef(
                url=url, speaker_key=speaker.key, title=title,
                pub_date=pub, source="regional_js",
            ))
        return refs


class MinneapolisParser(JsIndexParser):
    """Mpls: Kashkari publishes substantive written content as /article/<yyyy>/<slug>
    essays. The /speeches/<yyyy>/... pages on his profile are short Q&A
    descriptions (700-1700 chars) — descriptions, not transcripts — so we
    skip them. Date comes from the page's <meta name='ess:publishdatetime'>.
    """
    BODY_SELECTOR = "div.body-text"

    def _parse_body(self, soup: BeautifulSoup, ref: SpeechRef) -> _BodyParse:
        node = (
            _largest_paragraph_node(soup, "div.body-text")
            or _largest_paragraph_node(soup, "article")
            or _largest_paragraph_node(soup, "main")
        )
        if node is None:
            raise ValueError(f"No body wrapper at {ref.url}")
        return _BodyParse(
            date=_extract_meta_date(soup) or ref.pub_date,
            title=_extract_title(soup) or ref.title,
            body=extract_paragraphs(node),
        )

    def _parse_index(
        self, soup: BeautifulSoup, speaker: Speaker, since: dt.date | None
    ) -> list[SpeechRef]:
        refs: list[SpeechRef] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.match(r"/article/(\d{4})/([^?#\s\"']+)", href)
            if not m:
                continue
            year = int(m.group(1))
            if since and year < since.year:
                continue
            url = urljoin("https://www.minneapolisfed.org", href)
            if url in seen:
                continue
            seen.add(url)
            title = a.get_text(" ", strip=True) or m.group(2)
            refs.append(SpeechRef(
                url=url, speaker_key=speaker.key, title=title,
                pub_date=dt.date(year, 1, 1), source="regional_js",
            ))
        return refs


# ---- registry + entry points ------------------------------------------------


PARSERS: dict[str, BankParser] = {
    # Group A — RSS + HTML
    "atlanta_bostic":     AtlantaParser(),
    "richmond_barkin":    RichmondParser(),
    # Group B — server-rendered HTML index
    "boston_collins":     BostonParser(),
    "ny_williams":        NewYorkParser(),
    "sf_daly":            SanFranciscoParser(),
    # Group C — Playwright-rendered index pages
    "chicago_goolsbee":   ChicagoParser(),
    "cleveland_hammack":  ClevelandParser(),
    "stl_musalem":        StLouisParser(),
    "dallas_logan":       DallasParser(),
    "kc_schmid":          KansasCityParser(),
    "philly_paulson":     PhiladelphiaParser(),
    "mpls_kashkari":      MinneapolisParser(),
}


def discover_regional(
    speakers: list[Speaker], since: dt.date | None, *, pw=None
) -> list[SpeechRef]:
    """Iterate regional speakers and dispatch to their per-bank parser."""
    refs: list[SpeechRef] = []
    for sp in speakers:
        parser = PARSERS.get(sp.key)
        if parser is None:
            # Speaker is configured but parser isn't implemented yet (Group B/C
            # before they land). Skip silently so partial commits don't crash.
            continue
        try:
            sp_refs = parser.discover(sp, since, pw=pw)
        except Exception as exc:
            # One bank's outage shouldn't break the rest of the run.
            from ..utils.log import get_logger
            get_logger().exception(
                "regional discover failed for %s: %s", sp.key, exc
            )
            continue
        # Drop speeches dated before the speaker's tenure start. Regional bank
        # listings often expose the entire historical archive (Boston shows
        # Rosengren back to 2007; Richmond exposes Broaddus/Lacker URLs);
        # without this filter those get attributed to the current president.
        if sp.tenure_start is not None:
            sp_refs = [r for r in sp_refs if r.pub_date >= sp.tenure_start]
        refs.extend(sp_refs)
    return refs


def fetch_regional(ref: SpeechRef, *, pw=None) -> Speech:
    """Look up the parser by speaker_key and fetch one speech."""
    parser = PARSERS.get(ref.speaker_key)
    if parser is None:
        raise ValueError(
            f"No regional parser registered for speaker_key={ref.speaker_key!r}"
        )
    return parser.fetch(ref, pw=pw)
