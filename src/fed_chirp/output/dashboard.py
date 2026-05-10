"""Editorial-design dashboard renderer for Fed Chirp.

Phase 6: editorial redesign — paper-cream palette, Instrument Serif +
IBM Plex Sans + JetBrains Mono typography, marketing-style layout
(topbar + marquee ticker + hero with reading card + section frames +
method cards + CTA + footer). Single-file inline HTML/CSS/SVG; only
external assets are Google Fonts.

Mobile uses a single responsive layout with @media (max-width: 720px).
"""

from __future__ import annotations

import datetime as dt
import html
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..analysis.deltas import WINDOW_DAYS
from ..analysis import divergence as divergence_analysis
from ..analysis import futures as futures_analysis
from ..analysis.health import StaleSpeaker
from ..fetchers.federalreserve import Speaker
from ..fetchers.fomc import (
    DOC_MINUTES,
    DOC_PRESSER,
    DOC_STATEMENT,
    FOMC_SPEAKER_KEY,
    doc_type_label,
)
from ..storage.db import MarketReaction, ProcessingSkip, StoredScore


# ---------- constants ----------

SPARK_W = 220
SPARK_H = 36

# Region grouping per design (Board=7, East=4, Midwest=4, West=2, South=2 → 19)
_REGION_GROUP: dict[str, str] = {
    # Board (7)
    "powell": "Board", "jefferson": "Board", "bowman": "Board",
    "barr": "Board", "cook": "Board", "miran": "Board", "waller": "Board",
    # East coast (4)
    "ny_williams": "East coast", "boston_collins": "East coast",
    "philly_paulson": "East coast", "richmond_barkin": "East coast",
    # Midwest (4)
    "chicago_goolsbee": "Midwest", "cleveland_hammack": "Midwest",
    "mpls_kashkari": "Midwest", "kc_schmid": "Midwest",
    # West (2)
    "sf_daly": "West", "dallas_logan": "West",
    # South (2)
    "atlanta_bostic": "South", "stl_musalem": "South",
}

# Region short labels shown on speaker card top-right
_REGION_SHORT: dict[str, str] = {
    "powell": "FRB", "jefferson": "FRB", "bowman": "FRB", "barr": "FRB",
    "cook": "FRB", "miran": "FRB", "waller": "FRB",
    "ny_williams": "NY", "boston_collins": "BOS", "philly_paulson": "PHI",
    "richmond_barkin": "RIC", "cleveland_hammack": "CLE",
    "chicago_goolsbee": "CHI", "mpls_kashkari": "MPLS", "kc_schmid": "KC",
    "sf_daly": "SF", "dallas_logan": "DAL", "atlanta_bostic": "ATL",
    "stl_musalem": "STL",
}


# ---------- dataclasses ----------

@dataclass
class FuturesContext:
    """Futures-derived data passed in by cli.py."""
    chain: dict[str, float]
    chain_settle_date: dt.date | None
    upcoming_meetings: list[dt.date]
    current_rate: float | None


@dataclass
class _Meeting:
    """Group of FOMC docs (statement / presser / minutes) for one meeting."""
    meeting_date: dt.date
    statement: StoredScore | None = None
    presser: StoredScore | None = None
    minutes: StoredScore | None = None

    @property
    def combined(self) -> float | None:
        parts = [s.score for s in (self.statement, self.presser) if s is not None]
        return sum(parts) / len(parts) if parts else None

    @property
    def drift(self) -> float | None:
        if self.statement is None or self.presser is None:
            return None
        return self.presser.score - self.statement.score


# ---------- helpers ----------

_MEETING_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}(?:[–—\-]+(\d{1,2}))?,\s+(\d{4})"
)
_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 1)}


def _polarity_class(score: float | None) -> str:
    """t-hawk / t-dove / t-neutral (design's semantic color classes)."""
    if score is None:
        return "t-neutral"
    if score > 0.3:
        return "t-hawk"
    if score < -0.3:
        return "t-dove"
    return "t-neutral"


def _polarity_label(score: float | None) -> str:
    """'hawkish' / 'dovish' / 'neutral' — for chip text."""
    if score is None:
        return "neutral"
    if score > 0.3:
        return "hawkish"
    if score < -0.3:
        return "dovish"
    return "neutral"


def _short_date(d: dt.date) -> str:
    """'04·29' (mm·dd, design convention)."""
    return f"{d.month:02d}·{d.day:02d}"


def _full_dot_date(d: dt.date) -> str:
    """'2026–04–29' (en-dash separators per design)."""
    return f"{d.year}–{d.month:02d}–{d.day:02d}"


def _format_score(score: float | None, decimals: int = 2) -> str:
    """'+0.10' / '−0.30' / '—'. Uses Unicode minus."""
    if score is None:
        return "—"
    sign = "+" if score >= 0 else "−"
    return f"{sign}{abs(score):.{decimals}f}"


def _score_y_for_spark(score: float, h: float = SPARK_H) -> float:
    """Score → SVG y. y=h/2 is zero, lower y = more hawkish; clamped to [0,h]."""
    y = h / 2 - (score / 2) * (h / 2)
    return max(0.0, min(h, y))


def _spark_polyline_points(scores: list[float]) -> str:
    """Build polyline points for a 220x36 viewBox (oldest → newest)."""
    if not scores:
        return ""
    if len(scores) == 1:
        y = _score_y_for_spark(scores[0])
        return f"0,{y:.0f} {SPARK_W},{y:.0f}"
    pts = []
    for i, s in enumerate(scores):
        x = (i / (len(scores) - 1)) * SPARK_W
        y = _score_y_for_spark(s)
        pts.append(f"{x:.0f},{y:.0f}")
    return " ".join(pts)


def _parse_minutes_meeting_date(title: str) -> dt.date | None:
    m = _MEETING_DATE_RE.search(title)
    if not m:
        return None
    full = m.group(0)
    nums = re.findall(r"\d+", full)
    if not nums:
        return None
    cand_days = [int(n) for n in nums if int(n) <= 31]
    if not cand_days:
        return None
    day = cand_days[-1]
    try:
        return dt.date(int(m.group(3)), _MONTHS[m.group(1)], day)
    except (KeyError, ValueError):
        return None


def _group_by_meeting(fomc_scores: list[StoredScore]) -> list[_Meeting]:
    """Group FOMC docs into meetings keyed on meeting_date."""
    by_date: dict[dt.date, _Meeting] = {}
    for s in fomc_scores:
        if s.doc_type == "fomc_minutes":
            mdate = _parse_minutes_meeting_date(s.title) or s.speech_date
        else:
            mdate = s.speech_date
        m = by_date.setdefault(mdate, _Meeting(meeting_date=mdate))
        if s.doc_type == "fomc_statement":
            m.statement = s
        elif s.doc_type == "fomc_presser":
            m.presser = s
        elif s.doc_type == "fomc_minutes":
            m.minutes = s
    out = list(by_date.values())
    out.sort(key=lambda m: m.meeting_date, reverse=True)
    return out


def _polarity_clabel(score: float | None) -> str:
    """Italic editorial blurb under FOMC cell combined score."""
    if score is None:
        return "—"
    if score >= 1.0:
        return "Sharply hawkish."
    if score >= 0.5:
        return "Hawkish-leaning."
    if score >= 0.3:
        return "Modest hawkish lean."
    if score > -0.3:
        return "Steady, broadly neutral."
    if score > -0.5:
        return "Modest dovish lean."
    if score > -1.0:
        return "Dovish-leaning."
    return "Sharply dovish."


def _meeting_months_phrase(d1: dt.date, d2: dt.date) -> str:
    """Human-readable span between two meeting dates."""
    months = (d2.year - d1.year) * 12 + (d2.month - d1.month)
    if months <= 1:
        return "since last meeting"
    nums = ["one", "two", "three", "four", "five", "six", "seven", "eight",
            "nine", "ten", "eleven", "twelve"]
    return f"in {nums[months-1]} months" if months <= 12 else f"in {months} months"


def _render_inline_md(text: str) -> str:
    """Tiny markdown subset: **bold**, *italic*, '→'. HTML-escaped first."""
    safe = html.escape(text)
    safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", safe)
    return safe


def _short_role(role: str) -> str:
    """Compact role label for speaker cards."""
    if "Vice Chair for Supervision" in role:
        return "Vice Chair · Supervision"
    if role == "Chair":
        return "Chair · Board"
    if role == "Vice Chair":
        return "Vice Chair · Board"
    if role == "Governor":
        return "Governor · Board"
    if role == "President":
        return "President"
    if role == "Committee":
        return "FOMC committee"
    return role


# Tickers shown in the FOMC market-reaction matrix. ZT=F is rendered
# as approximate 2y yield Δ in basis points (sign-flipped, hawk/dove
# polarity); ES=F and NQ=F are rendered as price % change.
_REACTION_TICKERS: tuple[tuple[str, str], ...] = (
    ("ES=F", "S&P (ES)"),
    ("NQ=F", "Nasdaq (NQ)"),
    ("ZT=F", "2y yield (Δbp)"),
)
_YIELD_TICKERS: frozenset[str] = frozenset({"ZT=F"})
_ZT_DURATION = 2.0


def _fmt_pct_cell(pct: float | None) -> str:
    """Equity ticker cell: '+0.42%' / '−0.19%' / '—'.

    Uses market-data convention coloring inside the Market Reaction
    section: positive = green, negative = red. Distinct from the
    hawk/dove tone classes used elsewhere on the dashboard.
    """
    if pct is None:
        return '<span class="muted">—</span>'
    if abs(pct) < 0.005:
        cls = "t-neutral"
    elif pct > 0:
        cls = "t-up"
    else:
        cls = "t-down"
    sign = "+" if pct > 0 else "−" if pct < 0 else ""
    return f'<span class="{cls}">{sign}{abs(pct):.2f}%</span>'


def _fmt_yield_cell(pct: float | None, duration: float = _ZT_DURATION) -> str:
    """Bond ticker cell: '+3.5 bp' approx 2y yield Δ.

    Sign is flipped from price (price up = yield down), so the
    displayed bp number runs +ve when yields rose. Coloring follows
    the same plus-is-green / minus-is-red convention as the equity
    cells in this section, so a row of green or red reads
    consistently across all three tickers regardless of ticker
    semantics.
    """
    if pct is None:
        return '<span class="muted">—</span>'
    bp = -pct * 100.0 / duration
    if abs(bp) < 0.5:
        cls = "t-neutral"
    elif bp > 0:
        cls = "t-up"
    else:
        cls = "t-down"
    sign = "+" if bp > 0 else "−" if bp < 0 else ""
    return f'<span class="{cls}">{sign}{abs(bp):.1f} bp</span>'


def _fmt_reaction_cell(ticker: str, pct: float | None) -> str:
    if ticker in _YIELD_TICKERS:
        return _fmt_yield_cell(pct)
    return _fmt_pct_cell(pct)


_SKIP_REASON_LABELS: dict[str, str] = {
    "fetch_failed": "Fetch failed",
    "captions_unavailable": "YouTube captions unavailable",
    "filter_rejected": "Failed speech-likeness filter",
    "rubric_excluded": "Excluded by rubric (non-MP topic)",
    "duplicate": "Duplicate body (content-hash match)",
}


# ---------- render() ----------

def render(
    speakers: list[Speaker],
    scores: list[StoredScore],
    out_path: Path,
    futures_ctx: FuturesContext | None = None,
    reactions: list[MarketReaction] | None = None,
    stale: list[StaleSpeaker] | None = None,
    skips: list[ProcessingSkip] | None = None,
) -> None:
    """Render the editorial-design single-file dashboard.

    Same signature as before; new visual language. `reactions`, `stale`,
    `skips` are accepted for compatibility but not currently surfaced —
    the editorial design omits the explicit transparency panels in favor
    of a cleaner reader-facing layout.
    """
    speech_scores = [s for s in scores if s.doc_type == "speech"]
    fomc_scores = [s for s in scores if s.doc_type != "speech"]

    by_speaker_key: dict[str, list[StoredScore]] = defaultdict(list)
    for s in speech_scores:
        by_speaker_key[s.speaker_key].append(s)
    for k in by_speaker_key:
        by_speaker_key[k].sort(key=lambda x: x.speech_date)

    governor_speakers = [sp for sp in speakers if sp.key != FOMC_SPEAKER_KEY]
    speakers_by_key = {sp.key: sp for sp in speakers}
    meetings = _group_by_meeting(fomc_scores)

    now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    topbar_html = _topbar_section()
    ticker_html = _ticker_section(meetings, speech_scores, futures_ctx)
    hero_html = _hero_section(meetings, len(scores), len(governor_speakers), now_utc)
    pulse_html = _fomc_pulse_section(meetings)
    reactions_html = _reactions_section(reactions or [])
    market_path_html = _market_path_section(futures_ctx, meetings)
    committee_html = _committee_section(governor_speakers, scores, speakers_by_key)
    speakers_html = _speakers_section(governor_speakers, by_speaker_key)
    recent_html = _recent_section(speech_scores[:30], speakers_by_key)
    method_html = _method_section()
    transparency_html = _transparency_section(
        skips or [], stale or [], speakers_by_key
    )
    cta_html = _cta_section()
    footer_html = _footer_section(now_utc)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _PAGE.format(
            topbar=topbar_html,
            ticker=ticker_html,
            hero=hero_html,
            pulse=pulse_html,
            reactions=reactions_html,
            market_path=market_path_html,
            committee=committee_html,
            speakers=speakers_html,
            recent=recent_html,
            method=method_html,
            transparency=transparency_html,
            cta=cta_html,
            footer=footer_html,
            now_iso=now_utc.isoformat(),
        ),
        encoding="utf-8",
    )


# ---------- section: topbar ----------

def _topbar_section() -> str:
    return """
<header class="topbar">
  <a class="logomark" href="#">
    <span class="logomark__bird"></span>
    <span>Fed Chirp</span>
    <span class="brand-pill">Beta</span>
  </a>
  <nav class="topbar__nav">
    <a href="#fomc">FOMC pulse</a>
    <a href="#reactions">Reactions</a>
    <a href="#market-path">Market path</a>
    <a href="#committee">Committee</a>
    <a href="#speakers">Speakers</a>
    <a href="#recent">Recent</a>
    <a href="#method">Method</a>
  </nav>
  <div class="topbar__right">
    <span class="topbar__status"><span class="pulse-dot"></span>SCAN · 18:30 ET DAILY</span>
  </div>
  <button class="menubtn" aria-label="Open section menu" type="button">
    <span></span><span></span><span></span>
  </button>
</header>
"""


# ---------- section: ticker ----------

def _ticker_section(
    meetings: list[_Meeting],
    speech_scores: list[StoredScore],
    futures_ctx: FuturesContext | None,
) -> str:
    """Marquee with latest meeting + 6 most recent speakers + market data."""
    items: list[str] = []
    latest = meetings[0] if meetings else None
    if latest and latest.combined is not None:
        items.append(
            f'<span class="ticker__item"><span class="label">FOMC '
            f'{_short_date(latest.meeting_date)}</span>'
            f'<span class="{_polarity_class(latest.combined)}">{_format_score(latest.combined)}</span>'
            + (f'<span class="label">drift</span>'
               f'<span class="{_polarity_class(latest.drift)}">{_format_score(latest.drift)}</span>'
               if latest.drift is not None else '')
            + '</span>'
        )
    for s in speech_scores[:6]:
        last = s.speaker_key.rsplit("_", 1)[-1].upper()
        items.append(
            f'<span class="ticker__item"><span class="label">{html.escape(last)}</span>'
            f'<span class="{_polarity_class(s.score)}">{_format_score(s.score)}</span>'
            f'<span class="label">{_short_date(s.speech_date)}</span></span>'
        )
    if futures_ctx and futures_ctx.current_rate is not None:
        items.append(
            f'<span class="ticker__item"><span class="label">EFFR</span>'
            f'<span>{futures_ctx.current_rate:.3f}%</span></span>'
        )
        if futures_ctx.chain and futures_ctx.chain_settle_date:
            r12 = futures_analysis.implied_rate_n_months_out(
                futures_ctx.chain, 12, futures_ctx.chain_settle_date
            )
            if r12 is not None:
                bp = (r12 - futures_ctx.current_rate) * 100.0
                cls = "t-hawk" if bp > 0.5 else ("t-dove" if bp < -0.5 else "t-neutral")
                sign = "+" if bp >= 0 else "−"
                items.append(
                    f'<span class="ticker__item"><span class="label">12M IMPLIED</span>'
                    f'<span class="{cls}">{sign}{abs(bp):.1f} bp</span></span>'
                )

    if not items:
        return ""
    block = '<span class="ticker__sep">·</span>'.join(items)
    track = block + '<span class="ticker__sep">·</span>' + block
    return f"""
<div class="ticker" aria-hidden="true">
  <div class="ticker__track">{track}</div>
</div>
"""


# ---------- section: hero ----------

def _hero_section(
    meetings: list[_Meeting],
    total_docs: int,
    n_speakers: int,
    now: dt.datetime,
) -> str:
    latest = meetings[0] if meetings else None
    prior = next((m for m in meetings[1:] if m.combined is not None), None)

    if latest and latest.combined is not None:
        combined = latest.combined
        combined_class = _polarity_class(combined)
        polarity = _polarity_label(combined)
        clabel = _polarity_clabel(combined)
        needle_pos = max(0.0, min(100.0, (combined + 2) / 4 * 100))

        def _split_cell(label: str, score: StoredScore | None,
                         prior_score: StoredScore | None,
                         sub_text: str) -> str:
            if score is None:
                return f"""
              <div>
                <span class="eyebrow">{label}</span>
                <div class="val t-neutral">—</div>
              </div>"""
            delta_html = ""
            if prior_score is not None:
                d = score.score - prior_score.score
                arrow = "arrow-up" if d > 0 else ("arrow-down" if d < 0 else "arrow-flat")
                cls = "delta-up" if d > 0 else ("delta-down" if d < 0 else "delta-flat")
                delta_html = (
                    f'<div class="reading__delta {cls} {arrow}">{_format_score(d)} vs prior</div>'
                )
            return f"""
              <div>
                <span class="eyebrow">{label}</span>
                <div class="val {_polarity_class(score.score)}">{_format_score(score.score)}</div>
                <div class="vsub">{sub_text}</div>
                {delta_html}
              </div>"""

        stmt_sub = (latest.statement.speech_date.strftime("%b %-d, %Y")
                    if latest.statement else "")
        presser_sub = (f"{_short_date(latest.presser.speech_date)} · Powell Q&amp;A"
                       if latest.presser else "")
        stmt_html = _split_cell("Statement", latest.statement,
                                  prior.statement if prior else None, stmt_sub)
        presser_html = _split_cell("Press conference", latest.presser,
                                     prior.presser if prior else None, presser_sub)

        drift_html = ""
        if latest.drift is not None:
            drift_cls = _polarity_class(latest.drift)
            if abs(latest.drift) >= 0.3:
                drift_text = ("Powell's Q&amp;A pulled hawkish against the prepared text."
                               if latest.drift > 0
                               else "Powell's Q&amp;A pulled dovish against the prepared text.")
            else:
                drift_text = "Q&amp;A roughly tracked the statement."
            drift_html = f"""
              <div class="reading__driftrow">
                <span class="eyebrow">Drift · presser − statement</span>
                <div class="reading__driftval">
                  <span class="val {drift_cls}">{_format_score(latest.drift)}</span>
                  <span class="reading__driftnote">{drift_text}</span>
                </div>
              </div>"""

        reading_card = f"""
  <div class="reading">
    <div class="reading__top">
      <span class="eyebrow">Current FOMC stance · combined</span>
      <span class="chip"><span class="dot dot--{polarity}"></span>{polarity.title()}</span>
    </div>
    <div class="reading__big {combined_class}">{_format_score(combined)}</div>
    <div class="reading__label">{clabel}</div>
    <div class="reading__bar">
      <div class="tonebar">
        <div class="tonebar__tick" style="left: 50%;"></div>
        <div class="tonebar__needle"
             style="left: {needle_pos:.1f}%;"
             aria-label="Combined score {_format_score(combined)} on minus 2 to plus 2 scale"></div>
      </div>
      <div class="reading__barscale">
        <span>−2 dovish</span><span>0</span><span>+2 hawkish</span>
      </div>
    </div>
    <div class="reading__split">{stmt_html}{presser_html}{drift_html}</div>
  </div>
"""
    else:
        reading_card = """
  <div class="reading">
    <div class="reading__top">
      <span class="eyebrow">Current FOMC stance</span>
    </div>
    <div class="reading__big t-neutral">—</div>
    <div class="reading__label">No FOMC meeting scored yet.</div>
  </div>
"""

    last_regen_time = now.strftime("%H:%M UTC")
    last_regen_date = now.strftime("%b %-d, %Y")

    return f"""
<section class="hero">
  <div class="hero__left">
    <div class="eyebrow">A daily reading of every Federal Reserve voice</div>
    <h1 class="hero__head">Listening<br/>to the <em>Fed</em>,<br/>so you don't<br/>have to.</h1>
    <p class="hero__sub">
      Every speech, statement, minute, and press-conference transcript from the
      seven Board governors and twelve regional bank presidents — scored for
      hawkish or dovish tone against a fixed rubric, refreshed weekday evenings.
    </p>
    <div class="hero__cta">
      <a class="btn" href="#fomc">Open the latest reading →</a>
      <a class="btn btn--ghost" href="#method">How we score</a>
    </div>
    <div class="hero__meta">
      <div class="hero__metaitem">
        <span class="eyebrow">Documents scored</span>
        <div class="v">{total_docs}</div>
        <div class="vs">since Dec 2025</div>
      </div>
      <div class="hero__metaitem">
        <span class="eyebrow">Speakers tracked</span>
        <div class="v">{n_speakers}</div>
        <div class="vs">7 governors · 12 presidents</div>
      </div>
      <div class="hero__metaitem">
        <span class="eyebrow">Last regenerated</span>
        <time id="regen-time" datetime="{now.isoformat()}" class="v hero__metaitem--small">{last_regen_time}</time>
        <div class="vs">{last_regen_date}</div>
      </div>
    </div>
  </div>
  {reading_card}
</section>
"""


# ---------- section: FOMC pulse ----------

def _fomc_pulse_section(meetings: list[_Meeting]) -> str:
    if not meetings:
        return ""
    # Grid: latest first (left), oldest last (right).
    # _group_by_meeting() returns meetings sorted descending, so we just slice.
    cells = meetings[:4]
    cell_blocks: list[str] = []
    for i, m in enumerate(cells):
        is_latest = (i == 0)
        is_first = (i == len(cells) - 1)
        if is_latest:
            cur_label = "latest"
        elif is_first:
            cur_label = "first"
        else:
            cur_label = "hold"
        combined_html = (
            f'<div class="combined {_polarity_class(m.combined)}">{_format_score(m.combined)}</div>'
            if m.combined is not None else '<div class="combined">—</div>'
        )
        clabel = _polarity_clabel(m.combined)
        stmt_str = _format_score(m.statement.score) if m.statement else "—"
        stmt_cls = _polarity_class(m.statement.score) if m.statement else ""
        presser_str = _format_score(m.presser.score) if m.presser else "—"
        presser_cls = _polarity_class(m.presser.score) if m.presser else ""
        drift_str = _format_score(m.drift) if m.drift is not None else "—"
        drift_cls = _polarity_class(m.drift) if m.drift is not None else ""

        # In latest-first ordering, the prior (older) meeting is the next entry.
        prior = cells[i + 1] if i + 1 < len(cells) else None
        if prior and m.combined is not None and prior.combined is not None:
            d = m.combined - prior.combined
            delta_str = _format_score(d)
            delta_cls = _polarity_class(d)
        else:
            delta_str = "—"
            delta_cls = ""
        latest_class = " fomc-cell--latest" if is_latest else ""
        cell_blocks.append(f"""
    <div class="fomc-cell{latest_class}">
      <span class="date">{_full_dot_date(m.meeting_date)}</span><span class="cur">{cur_label}</span>
      {combined_html}
      <div class="clabel">{clabel}</div>
      <div class="stack">
        <span class="k">Statement</span><span class="v {stmt_cls}">{stmt_str}</span>
        <span class="k">Presser</span><span class="v {presser_cls}">{presser_str}</span>
        <span class="k">Drift</span><span class="v {drift_cls}">{drift_str}</span>
        <span class="k">Δ vs prior</span><span class="v {delta_cls}">{delta_str}</span>
      </div>
    </div>""")

    # Trajectory chart still reads time left-to-right (oldest → newest), so
    # pass a reversed copy of the cells list.
    traj_svg = _trajectory_svg(list(reversed(cells)))
    notes_html = _diff_notes_aside(meetings[0])

    return f"""
<section class="section" id="fomc">
  <div class="section__head">
    <div>
      <div class="section__num">§ 01 — FOMC pulse</div>
      <h2 class="section__title">Per-meeting <em>combined</em> view.</h2>
    </div>
    <p class="section__lede">
      Each meeting collapses to one combined score: the prepared statement
      averaged with Powell's same-day press conference. Drift is what the
      Q&amp;A added on top of the text. Δ vs prior compares this meeting's
      combined score to the previous meeting's.
    </p>
  </div>
  <div class="fomc-grid">{"".join(cell_blocks)}</div>
  <div class="fomc-after">
    {traj_svg}
    {notes_html}
  </div>
</section>
"""


def _trajectory_svg(cells: list[_Meeting]) -> str:
    """SVG trajectory of combined scores; cells are oldest → newest."""
    valid = [(i, m) for i, m in enumerate(cells) if m.combined is not None]
    if len(valid) < 2:
        return ""
    n = len(cells)
    x_left, x_right = 60, 700
    y_top, y_bot = 5, 195
    y_zero = (y_top + y_bot) / 2

    def x_for(i: int) -> float:
        if n == 1:
            return (x_left + x_right) / 2
        return x_left + (i / (n - 1)) * (x_right - x_left)

    def y_for(score: float) -> float:
        c = max(-2.0, min(2.0, score))
        return y_zero - (c / 2) * (y_zero - y_top)

    points = [(x_for(i), y_for(m.combined), m) for i, m in valid]
    polyline = " ".join(f"{x:.0f},{y:.0f}" for x, y, _ in points)
    area_d = f"M {points[0][0]:.0f} {y_zero:.0f} "
    for x, y, _ in points:
        area_d += f"L {x:.0f} {y:.0f} "
    area_d += f"L {points[-1][0]:.0f} {y_zero:.0f} Z"

    circles, score_labels, date_labels = [], [], []
    for i, (x, y, m) in enumerate(points):
        is_last = (i == len(points) - 1)
        cls = _polarity_class(m.combined)
        fill = ("var(--hawk)" if cls == "t-hawk"
                else "var(--dove)" if cls == "t-dove"
                else ("var(--ink)" if is_last else "var(--neutral)"))
        radius = 6 if is_last else 5
        circles.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{radius}" fill="{fill}"/>')
        label_y = y - 6 if y < y_zero else y + 18
        anchor = "end" if is_last else "middle"
        score_labels.append(
            f'<text x="{x:.0f}" y="{label_y:.0f}" text-anchor="{anchor}" '
            f'font-family="Instrument Serif" font-size="16" fill="var(--ink)">'
            f'{_format_score(m.combined)}</text>'
        )
        date_labels.append(
            f'<text x="{x:.0f}" y="210" text-anchor="middle" '
            f'font-family="JetBrains Mono" font-size="10" fill="var(--mute)" letter-spacing="0.05em">'
            f'{_short_date(m.meeting_date)}</text>'
        )

    first_combined = points[0][2].combined
    last_combined = points[-1][2].combined
    span_phrase = _meeting_months_phrase(points[0][2].meeting_date,
                                          points[-1][2].meeting_date)

    return f"""
    <div class="traj">
      <div class="traj__head">
        <div>
          <div class="eyebrow">Combined-score trajectory · last {len(points)} meetings</div>
          <div class="traj__phrase">
            From <span class="{_polarity_class(first_combined)}">{_format_score(first_combined)}</span>
            to <span class="{_polarity_class(last_combined)}">{_format_score(last_combined)}</span>
            <span class="traj__phrase-sub">{span_phrase}</span>
          </div>
        </div>
      </div>
      <svg viewBox="0 0 720 220" class="traj__svg">
        <line x1="0" y1="{y_zero:.0f}" x2="720" y2="{y_zero:.0f}" stroke="var(--rule)" stroke-width="1" stroke-dasharray="3 4"/>
        <text x="6" y="14" font-family="JetBrains Mono" font-size="10" fill="var(--mute)" letter-spacing="0.1em">+1.0</text>
        <text x="6" y="{y_zero+4:.0f}" font-family="JetBrains Mono" font-size="10" fill="var(--mute)" letter-spacing="0.1em">0</text>
        <text x="6" y="194" font-family="JetBrains Mono" font-size="10" fill="var(--mute)" letter-spacing="0.1em">−1.0</text>
        <path d="{area_d}" fill="var(--ink)" fill-opacity="0.06"/>
        <polyline points="{polyline}" fill="none" stroke="var(--ink)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        {"".join(circles)}
        <g>{"".join(score_labels)}</g>
        <g>{"".join(date_labels)}</g>
      </svg>
    </div>"""


def _diff_notes_aside(latest: _Meeting) -> str:
    if not latest or not latest.statement or not latest.statement.diff_notes:
        return ""
    notes = latest.statement.diff_notes[:5]
    if not notes:
        return ""
    items = "".join(f"<li>{_render_inline_md(n)}</li>" for n in notes)
    return f"""
    <aside class="aside">
      <div class="eyebrow">What changed at {_short_date(latest.meeting_date)}</div>
      <div class="aside__title">Wording shifts and dissents in the latest statement.</div>
      <ul class="aside__list">{items}</ul>
    </aside>"""


# ---------- section: FOMC market reactions ----------

def _reactions_section(reactions: list[MarketReaction]) -> str:
    """§ 02 — FOMC market reaction.

    ES/NQ/ZT moves around each meeting in three windows:
    statement (14:00→14:30 ET), same-day close (14:30→16:00 ET),
    next-day close (→ next 16:00 ET). Mobile collapses each meeting
    into a card with stacked label:value rows.
    """
    if not reactions:
        return ""

    # Pivot meeting_date → ticker → MarketReaction
    by_meeting: dict[dt.date, dict[str, MarketReaction]] = {}
    for r in reactions:
        by_meeting.setdefault(r.meeting_date, {})[r.ticker] = r
    if not by_meeting:
        return ""

    windows: tuple[tuple[str, str, str], ...] = (
        ("stmt", "stmt", "Statement window · 14:00→14:30 ET"),
        ("eod", "EOD", "Same-day close · 14:30→16:00 ET"),
        ("nextday", "next-day", "Next-day close · → next 16:00 ET"),
    )
    n_tickers = len(_REACTION_TICKERS)

    rows: list[str] = []
    for md in sorted(by_meeting.keys(), reverse=True):
        per_ticker = by_meeting[md]
        tds: list[str] = [
            f'<td class="rxn__meeting" data-label="Meeting">{md.isoformat()}</td>'
        ]
        for w_idx, (prefix, window_short, _full) in enumerate(windows):
            for t_idx, (ticker, ticker_label) in enumerate(_REACTION_TICKERS):
                r = per_ticker.get(ticker)
                pct = getattr(r, f"{prefix}_pct_change", None) if r else None
                cell = _fmt_reaction_cell(ticker, pct)
                short_ticker = ticker_label.split()[0]  # "S&P", "Nasdaq", "2y"
                data_label = f"{short_ticker} {window_short}"
                divider = " rxn__divider" if (w_idx > 0 and t_idx == 0) else ""
                tds.append(
                    f'<td class="{divider}" data-label="{html.escape(data_label)}">{cell}</td>'
                )
        rows.append("<tr>" + "".join(tds) + "</tr>")

    ticker_th = "".join(
        f'<th class="rxn__th">{html.escape(label)}</th>'
        for _, label in _REACTION_TICKERS
    )

    return f"""
<section class="section section--paper2" id="reactions">
  <div class="section__head">
    <div>
      <div class="section__num">§ 02 — FOMC market reaction</div>
      <h2 class="section__title">Where rates and<br/>equities <em>moved</em>.</h2>
    </div>
    <p class="section__lede">
      Intraday moves around each FOMC meeting. ES and NQ as price % change
      with annualized realized vol; ZT as approximate 2y yield Δ in basis
      points (positive bp = hawkish, derived via duration ≈ 2.0).
    </p>
  </div>
  <div class="rxn-wrap">
    <table class="rxn cards">
      <thead>
        <tr>
          <th class="rxn__meeting" rowspan="2">Meeting</th>
          <th class="rxn__group" colspan="{n_tickers}">Statement window<br/><span class="rxn__time">14:00→14:30 ET</span></th>
          <th class="rxn__group" colspan="{n_tickers}">Same-day close<br/><span class="rxn__time">14:30→16:00 ET</span></th>
          <th class="rxn__group" colspan="{n_tickers}">Next-day close<br/><span class="rxn__time">→ next 16:00 ET</span></th>
        </tr>
        <tr>{ticker_th}{ticker_th}{ticker_th}</tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
</section>
"""


# ---------- section: market-implied path ----------

def _market_path_section(
    ctx: FuturesContext | None,
    meetings: list[_Meeting],
) -> str:
    """§ 03 — Market-implied path. Current EFFR, 6/12-month implied,
    gap-vs-Fedspeak, per-meeting probability buckets."""
    if ctx is None or not ctx.chain or ctx.current_rate is None:
        return ""

    today = ctx.chain_settle_date or dt.date.today()
    cur = ctx.current_rate
    r6 = futures_analysis.implied_rate_n_months_out(ctx.chain, 6, today)
    r12 = futures_analysis.implied_rate_n_months_out(ctx.chain, 12, today)

    def _rate_row(label: str, r: float | None, sub: str = "") -> str:
        if r is None:
            return f"""
        <div class="path__row">
          <div class="eyebrow">{label}</div>
          <div class="path__rate"><span class="muted">—</span></div>
        </div>"""
        delta_bp = (r - cur) * 100.0
        if label.lower().startswith("effective"):
            delta_html = ""
        else:
            sign = "+" if delta_bp >= 0 else "−"
            cls = ("t-hawk" if delta_bp > 0.5
                   else "t-dove" if delta_bp < -0.5
                   else "t-neutral")
            delta_html = (f'<span class="path__delta {cls}">'
                          f'{sign}{abs(delta_bp):.1f} bp vs current</span>')
        return f"""
        <div class="path__row">
          <div class="eyebrow">{label}</div>
          <div class="path__rate"><strong>{r:.3f}%</strong>{delta_html}
            {f'<div class="path__sub">{sub}</div>' if sub else ''}
          </div>
        </div>"""

    rates_html = (
        _rate_row("Effective fed funds rate (current)", cur)
        + _rate_row("Market-implied, 6 months out", r6)
        + _rate_row("Market-implied, 12 months out", r12)
    )

    # Gap vs latest FOMC tone
    fed_speak: float | None = None
    fed_speak_label = "no recent FOMC docs"
    for m in meetings:
        if m.combined is not None:
            fed_speak = m.combined
            fed_speak_label = f"meeting {m.meeting_date.isoformat()}"
            break

    gap_html = ""
    if fed_speak is not None and r12 is not None:
        ms, _ = futures_analysis.market_score(ctx.chain, cur, horizon_months=12)
        gap = fed_speak - ms
        gap_cls = (
            "t-hawk" if gap > 0.3 else "t-dove" if gap < -0.3 else "t-neutral"
        )
        gap_phrase = (
            "Fed-speak more hawkish than market"
            if gap > 0.3
            else "Fed-speak more dovish than market"
            if gap < -0.3
            else "Fed-speak roughly aligned with market"
        )
        gap_html = f"""
    <div class="path__gap">
      <div class="eyebrow">Gap · Fed-speak vs market</div>
      <div class="path__gap-row">
        <span class="path__gap-pair">Fed-speak ({html.escape(fed_speak_label)})
          <span class="{_polarity_class(fed_speak)}">{_format_score(fed_speak)}</span></span>
        <span class="path__gap-pair">Market score
          <span class="{_polarity_class(ms)}">{_format_score(ms)}</span></span>
      </div>
      <div class="path__gap-bottom">
        <span class="path__gap-delta {gap_cls}">{_format_score(gap)}</span>
        <span class="path__gap-phrase">{gap_phrase}</span>
      </div>
    </div>"""

    # Per-meeting probability table
    probs_html = ""
    if ctx.upcoming_meetings:
        try:
            meeting_rates = futures_analysis.implied_rates_at_meetings(
                ctx.chain, ctx.upcoming_meetings, cur
            )
        except Exception:
            meeting_rates = []
        rows_data: list[tuple] = []
        bucket_set: set[float] = set()
        for mr in meeting_rates:
            p = futures_analysis.move_probabilities(mr)
            rows_data.append((mr, p))
            for b in p.buckets:
                bucket_set.add(b)
        if rows_data:
            buckets = sorted(bucket_set)
            header_cells = "".join(
                f'<th class="rxn__th">{html.escape(_bucket_label(b))}</th>'
                for b in buckets
            )
            body_rows: list[str] = []
            for mr, p in rows_data:
                cells = []
                for b in buckets:
                    v = p.buckets.get(b, 0.0)
                    bucket_label = _bucket_label(b)
                    cls_strength = (
                        "bucket-strong" if v > 0.5
                        else "bucket-mod" if v > 0.2
                        else "bucket-weak" if v > 0
                        else "bucket-zero"
                    )
                    cells.append(
                        f'<td class="bucket-cell {cls_strength}" '
                        f'data-label="{html.escape(bucket_label)}">{v*100:.0f}%</td>'
                    )
                d_cls = _polarity_class(-mr.delta_bp / 50)
                body_rows.append(f"""
                <tr>
                  <td data-label="Meeting" class="rxn__meeting">{mr.meeting_date.isoformat()}</td>
                  <td data-label="Implied rate after"><strong>{mr.rate_after:.3f}%</strong></td>
                  <td data-label="Δ at meeting" class="{d_cls}">{mr.delta_bp:+.1f} bp</td>
                  {''.join(cells)}
                </tr>""")
            probs_html = f"""
    <h3 class="path__h3">Per-meeting probability buckets</h3>
    <div class="rxn-wrap">
      <table class="rxn cards">
        <thead><tr>
          <th class="rxn__meeting">Meeting</th>
          <th class="rxn__th">Implied rate after</th>
          <th class="rxn__th">Δ at meeting</th>
          {header_cells}
        </tr></thead>
        <tbody>{''.join(body_rows)}</tbody>
      </table>
    </div>"""

    asof = (f"as of {ctx.chain_settle_date.isoformat()}"
            if ctx.chain_settle_date else "as of today")

    return f"""
<section class="section" id="market-path">
  <div class="section__head">
    <div>
      <div class="section__num">§ 03 — Market-implied path</div>
      <h2 class="section__title">What futures<br/><em>price in</em>.</h2>
    </div>
    <p class="section__lede">
      The fed funds futures curve, broken into per-meeting probabilities via
      step-path / FedWatch math. Compared against the latest combined Fed-speak
      score to surface gaps. {asof}.
    </p>
  </div>
  <div class="path__rates">{rates_html}</div>
  {gap_html}
  {probs_html}
</section>
"""


def _bucket_label(bp: float) -> str:
    if bp == 0:
        return "Hold"
    word = "Hike" if bp > 0 else "Cut"
    return f"{word} {abs(int(bp))} bp"


# ---------- section: committee divergence ----------

def _committee_section(
    speakers: list[Speaker],
    scores: list[StoredScore],
    speakers_by_key: dict[str, Speaker],
) -> str:
    speaker_keys = [sp.key for sp in speakers if sp.key != FOMC_SPEAKER_KEY]
    if not speaker_keys:
        return ""
    today = dt.date.today()
    snap = divergence_analysis.divergence_snapshot(scores, speaker_keys, today)
    snap_30 = divergence_analysis.divergence_snapshot(
        scores, speaker_keys, today - dt.timedelta(days=30)
    )
    spread_change = snap.spread - snap_30.spread
    if abs(spread_change) >= 0.05 and snap_30.n_covered > 0:
        arrow = "↓" if spread_change < 0 else "↑"
        cls = "t-dove" if spread_change < 0 else "t-hawk"
        change_html = f' <span class="spread-change {cls}">{arrow} {abs(spread_change):.2f} vs 30d</span>'
    else:
        change_html = ""

    def _pole_html(key: str | None, color_cls: str) -> tuple[str, str, str]:
        if not key:
            return "—", "", ""
        sp = speakers_by_key.get(key)
        if not sp:
            return key, "", ""
        score_val = next((s.mean for s in snap.speakers if s.speaker_key == key), 0.0)
        short = _REGION_SHORT.get(key, sp.region.upper() if sp.region else "")
        return sp.name, _format_score(score_val), short

    hawk_name, hawk_score, hawk_short = _pole_html(snap.hawk_key, "t-hawk")
    dove_name, dove_score, dove_short = _pole_html(snap.dove_key, "t-dove")

    hawks, neutrals, doves = divergence_analysis.camps(snap)

    def _camp_row(s) -> str:
        sp = speakers_by_key.get(s.speaker_key)
        if not sp:
            return ""
        short = _REGION_SHORT.get(s.speaker_key, sp.region.upper() if sp.region else "")
        return f"""
      <div class="camp__row">
        <div><span class="pname">{html.escape(sp.name)}</span><span class="role">{html.escape(short)}</span></div>
        <span class="pscore {_polarity_class(s.mean)}">{_format_score(s.mean)}</span>
      </div>"""

    hawks_html = "".join(_camp_row(s) for s in hawks)
    neutrals_html = "".join(_camp_row(s) for s in neutrals)
    doves_html = "".join(_camp_row(s) for s in doves)

    if not doves:
        last_dove_msg = "—"
        for s in scores:
            if s.doc_type == "speech" and s.score < -0.3:
                sp = speakers_by_key.get(s.speaker_key)
                if sp:
                    last_dove_msg = (
                        f"Last dove: {html.escape(sp.name)} · {_format_score(s.score)}"
                        f"<br/>{s.speech_date.strftime('%b %-d, %Y')}"
                    )
                break
        doves_html = f"""
      <div class="camp__empty">
        <div class="camp__empty-text">No active doves on the trailing 90-day window.</div>
        <div class="camp__empty-sub">{last_dove_msg}</div>
      </div>"""

    ts = divergence_analysis.time_series(scores, speaker_keys, end_date=today,
                                          days_back=90)
    spread_spark = _spread_spark_svg([v for _, v in ts])

    return f"""
<section class="section section--paper2" id="committee">
  <div class="section__head">
    <div>
      <div class="section__num">§ 02 — Committee divergence</div>
      <h2 class="section__title">Where each<br/>voice <em>stands</em>.</h2>
    </div>
    <p class="section__lede">
      Each speaker's trailing 90-day mean places them in a camp. Widening
      spreads historically precede dissents at the next FOMC meeting.
    </p>
  </div>
  <div class="committee-strip">
    <div>
      <div class="eyebrow">Spread (max−min)</div>
      <div class="strip__big">{snap.spread:.2f}{change_html}</div>
    </div>
    <div>
      <div class="eyebrow">Stdev of speaker means</div>
      <div class="strip__big">{snap.stdev:.2f}</div>
    </div>
    <div>
      <div class="eyebrow">Hawk pole · 90d</div>
      <div class="strip__pole t-hawk">{html.escape(hawk_name)}<br/><span class="strip__poletag">{hawk_score} · {html.escape(hawk_short)}</span></div>
    </div>
    <div>
      <div class="eyebrow">Dove pole · 90d</div>
      <div class="strip__pole t-dove">{html.escape(dove_name)}<br/><span class="strip__poletag">{dove_score} · {html.escape(dove_short)}</span></div>
    </div>
  </div>
  <div class="camps">
    <div class="camp camp--hawk">
      <div class="camp__head"><div class="camp__name">Hawks</div><div class="camp__count">&gt; +0.30 · n={len(hawks)}</div></div>
      {hawks_html}
    </div>
    <div class="camp">
      <div class="camp__head"><div class="camp__name">Neutrals</div><div class="camp__count">−0.30 to +0.30 · n={len(neutrals)}</div></div>
      {neutrals_html}
      <div class="camp__poles">
        Spread, trailing 90d
        {spread_spark}
      </div>
    </div>
    <div class="camp camp--dove">
      <div class="camp__head"><div class="camp__name">Doves</div><div class="camp__count">&lt; −0.30 · n={len(doves)}</div></div>
      {doves_html}
    </div>
  </div>
</section>
"""


def _spread_spark_svg(values: list[float]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        values = [values[0], values[0]]
    lo = min(0.0, min(values))
    hi = max(values) if max(values) > 0 else 1.0
    pad = max(0.05, (hi - lo) * 0.1)
    lo -= pad
    hi += pad
    w, h = 220, 36
    pts = []
    for i, v in enumerate(values):
        x = (i / (len(values) - 1)) * w
        y = (1 - (v - lo) / (hi - lo)) * h
        pts.append(f"{x:.0f},{y:.0f}")
    last_x, last_y = pts[-1].split(",")
    return f"""
        <svg viewBox="0 0 {w} {h}" style="width:100%;height:48px;margin-top:8px;">
          <line x1="0" y1="{h/2:.0f}" x2="{w}" y2="{h/2:.0f}" stroke="var(--rule)" stroke-width="1" stroke-dasharray="2 3"/>
          <polyline fill="none" stroke="var(--ink)" stroke-width="1.5" points="{' '.join(pts)}"/>
          <circle cx="{last_x}" cy="{last_y}" r="3" fill="var(--ink)"/>
        </svg>"""


# ---------- section: speakers grid ----------

def _speakers_section(
    speakers: list[Speaker],
    by_speaker_key: dict[str, list[StoredScore]],
) -> str:
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=90)

    cards = []
    for sp in speakers:
        if sp.key == FOMC_SPEAKER_KEY:
            continue
        all_scores = by_speaker_key.get(sp.key, [])
        in_window = [s for s in all_scores if s.speech_date >= cutoff]
        if in_window:
            mean_90 = sum(s.score for s in in_window) / len(in_window)
            sub = f"90d · n={len(in_window)}"
            score_for_card = mean_90
        elif all_scores:
            score_for_card = all_scores[-1].score
            sub = f"last · {_short_date(all_scores[-1].speech_date)}"
        else:
            score_for_card = 0.0
            sub = "no data"

        score_cls = _polarity_class(score_for_card)
        spark_class = ("spark--hawk" if score_cls == "t-hawk"
                        else "spark--dove" if score_cls == "t-dove"
                        else "")
        spark_scores = [s.score for s in all_scores[-30:]]
        if not spark_scores:
            spark_html = '<svg class="spark sp__spark" viewBox="0 0 220 36"></svg>'
        else:
            polyline_pts = _spark_polyline_points(spark_scores)
            last_y = _score_y_for_spark(spark_scores[-1])
            spark_html = f"""<svg class="spark sp__spark {spark_class}" viewBox="0 0 220 36">
          <line class="spark__zero" x1="0" y1="18" x2="220" y2="18"/>
          <polyline class="spark__line" points="{polyline_pts}"/>
          <circle class="spark__dot" cx="220" cy="{last_y:.0f}" r="2.5"/>
        </svg>"""

        region_short = _REGION_SHORT.get(sp.key, "")
        region_group = _REGION_GROUP.get(sp.key, "Other")
        region_slug = region_group.lower().replace(" ", "-")
        cards.append(f"""
    <div class="sp" data-region="{html.escape(region_slug)}">
      <div class="sp__top">
        <div>
          <div class="sp__name">{html.escape(sp.name)}</div>
          <div class="sp__role">{html.escape(_short_role(sp.role))}</div>
        </div>
        <div class="sp__region">{html.escape(region_short)}</div>
      </div>
      <div class="sp__bot">
        <div>
          <div class="v {score_cls}">{_format_score(score_for_card)}</div>
          <div class="vsub">{sub}</div>
        </div>
        {spark_html}
      </div>
    </div>""")

    counts: dict[str, int] = defaultdict(int)
    for sp in speakers:
        if sp.key == FOMC_SPEAKER_KEY:
            continue
        counts[_REGION_GROUP.get(sp.key, "Other")] += 1
    total = sum(counts.values())

    chips = (
        f'<button class="chip chip--active" data-region="all" type="button">All · {total}</button>'
        f'<button class="chip" data-region="board" type="button">Board · {counts.get("Board", 0)}</button>'
        f'<button class="chip" data-region="east-coast" type="button">East coast · {counts.get("East coast", 0)}</button>'
        f'<button class="chip" data-region="midwest" type="button">Midwest · {counts.get("Midwest", 0)}</button>'
        f'<button class="chip" data-region="west" type="button">West · {counts.get("West", 0)}</button>'
        f'<button class="chip" data-region="south" type="button">South · {counts.get("South", 0)}</button>'
    )

    return f"""
<section class="section" id="speakers">
  <div class="section__head">
    <div>
      <div class="section__num">§ 03 — Speakers</div>
      <h2 class="section__title">Nineteen <em>voices</em>,<br/>one rubric.</h2>
    </div>
    <p class="section__lede">
      Seven Federal Reserve Board governors and twelve regional bank
      presidents. Each card shows the speaker's 90-day mean and a sparkline
      of their last thirty speech scores (oldest → newest, on the
      −2…+2 scale).
    </p>
  </div>
  <div class="chips-row">{chips}</div>
  <div class="speakers-grid">{"".join(cards)}</div>
</section>
"""


# ---------- section: recent feed ----------

def _recent_section(
    scores: list[StoredScore],
    speakers_by_key: dict[str, Speaker],
) -> str:
    if not scores:
        return ""
    half = (len(scores) + 1) // 2
    col1, col2 = scores[:half], scores[half:]

    def _row(s: StoredScore) -> str:
        sp = speakers_by_key.get(s.speaker_key)
        name = sp.name if sp else s.speaker_key
        title = s.title or ""
        rationale = s.rationale or ""
        if len(rationale) > 200:
            rationale = rationale[:200].rsplit(" ", 1)[0] + "…"
        cls = _polarity_class(s.score)
        polarity = _polarity_label(s.score)
        date_dot = f"{s.speech_date.year}·{s.speech_date.month:02d}·{s.speech_date.day:02d}"
        return f"""
      <div class="feed__row">
        <div class="feed__date">{date_dot}</div>
        <div>
          <div class="feed__name">{html.escape(name)}</div>
          <div class="feed__title">{html.escape(title)}</div>
          <p class="feed__quote">{html.escape(rationale)}</p>
        </div>
        <div>
          <div class="feed__score {cls}">{_format_score(s.score)}</div>
          <div class="feed__scoresub">{polarity}</div>
        </div>
      </div>"""

    return f"""
<section class="section" id="recent">
  <div class="section__head">
    <div>
      <div class="section__num">§ 04 — Recent</div>
      <h2 class="section__title">The last <em>thirty</em>,<br/>scored.</h2>
    </div>
    <p class="section__lede">
      Each entry shows the score, a one-sentence rationale extracted from the
      rubric pass, and the source link.
    </p>
  </div>
  <div class="feed">
    <div>{"".join(_row(s) for s in col1)}</div>
    <div>{"".join(_row(s) for s in col2)}</div>
  </div>
</section>
"""


# ---------- section: method ----------

def _method_section() -> str:
    return """
<section class="section section--paper2" id="method">
  <div class="section__head">
    <div>
      <div class="section__num">§ 05 — Method</div>
      <h2 class="section__title">How a speech<br/>becomes a <em>number</em>.</h2>
    </div>
    <p class="section__lede">
      Each document is scored by Claude Sonnet 4.6 against a fixed rubric.
      The rubric stays cached across scans; the only thing that changes is
      the doc-type header on the user message — speech vs statement vs
      minutes vs press-conference transcript.
    </p>
  </div>
  <div class="method-grid">
    <div class="method__card">
      <div class="step">Step 01</div>
      <h4>Discover</h4>
      <p>Per-speaker RSS feeds for governors; per-bank scrapers (RSS, server-rendered HTML, or headless Chromium) for the twelve regional presidents. FOMC docs from the consolidated press feed.</p>
    </div>
    <div class="method__card">
      <div class="step">Step 02</div>
      <h4>Score</h4>
      <p>A −2 (very dovish) to +2 (very hawkish) scale. Prompt caching keeps the rubric warm; doc-type header switches per file. Steady-state cost: about $5–10 a year.</p>
    </div>
    <div class="method__card">
      <div class="step">Step 03</div>
      <h4>Annotate</h4>
      <p>For each new FOMC statement, a separate Claude pass produces 3–5 bullet diff notes against the previous statement, naming the specific wording shifts that matter.</p>
    </div>
    <div class="method__card">
      <div class="step">Step 04</div>
      <h4>Alert</h4>
      <p>Speeches alert when |score − 90-day mean| ≥ 1.0 or |z| ≥ 1.5. FOMC docs alert at |Δ vs prior| ≥ 0.5. Email digest is dispatched only when something fires.</p>
    </div>
  </div>
</section>
"""


# ---------- section: transparency ----------

def _transparency_section(
    skips: list[ProcessingSkip],
    stale: list[StaleSpeaker],
    speakers_by_key: dict[str, Speaker],
) -> str:
    """§ 08 — Transparency: speakers gone quiet + speeches not scored."""
    if not skips and not stale:
        return ""

    # Coverage health (left column)
    health_html = ""
    if stale:
        rows = []
        for s in stale:
            if s.last_speech_date is None:
                last_str = "<em>never</em>"
                silent_str = "—"
            else:
                last_str = s.last_speech_date.isoformat()
                silent_str = f"{s.days_silent} days"
            rows.append(f"""
        <div class="trans__row">
          <div>
            <div class="trans__name">{html.escape(s.speaker.name)}</div>
            <div class="trans__sub">{html.escape(s.speaker.region)}</div>
          </div>
          <div class="trans__right">
            <div class="trans__val">{silent_str}</div>
            <div class="trans__sub">last · {last_str}</div>
          </div>
        </div>""")
        health_html = f"""
      <div class="trans__col">
        <h3 class="trans__h3">Coverage gone quiet</h3>
        <p class="trans__lede">
          Speakers whose latest stored speech is more than 60 days old. Could
          mean a scraper broke or just that the speaker hasn't posted a
          transcript-archived speech (TV/podcast appearances are intentionally
          excluded).
        </p>
        <div class="trans__list">{"".join(rows)}</div>
      </div>"""

    # Skips (right column) — group by reason for compactness
    skips_html = ""
    if skips:
        by_reason: dict[str, list[ProcessingSkip]] = defaultdict(list)
        for s in skips:
            by_reason[s.reason].append(s)
        # Show a per-reason counter then list a few examples
        groups = []
        for reason, items in sorted(
            by_reason.items(), key=lambda kv: -len(kv[1])
        ):
            label = _SKIP_REASON_LABELS.get(reason, reason)
            examples = []
            for sk in items[:5]:
                sp = speakers_by_key.get(sk.speaker_key)
                name = sp.name if sp else sk.speaker_key
                date_str = sk.pub_date.isoformat() if sk.pub_date else "—"
                examples.append(f"""
            <div class="trans__row">
              <div>
                <div class="trans__name">{html.escape(name)}</div>
                <div class="trans__sub">{html.escape(date_str)}</div>
              </div>
            </div>""")
            extra = (f'<div class="trans__more">+ {len(items)-5} more</div>'
                     if len(items) > 5 else "")
            groups.append(f"""
        <details class="trans__group">
          <summary>
            <span class="trans__reason">{html.escape(label)}</span>
            <span class="trans__count">{len(items)}</span>
          </summary>
          <div class="trans__list">{"".join(examples)}{extra}</div>
        </details>""")
        skips_html = f"""
      <div class="trans__col">
        <h3 class="trans__h3">Speeches not scored, last 90 days</h3>
        <p class="trans__lede">
          What the pipeline saw but excluded. Each speaker's hawk/dove mean
          is computed only from items that DO have a score, so it's worth
          knowing what was deliberately left out.
        </p>
        {"".join(groups)}
      </div>"""

    return f"""
<section class="section" id="transparency">
  <div class="section__head">
    <div>
      <div class="section__num">§ 08 — Transparency</div>
      <h2 class="section__title">What we <em>didn't</em><br/>score, and why.</h2>
    </div>
    <p class="section__lede">
      An open ledger of pipeline gaps and exclusions. Every speaker's mean
      is computed only from items that DO have a score; this section lists
      the rest.
    </p>
  </div>
  <div class="trans-grid">
    {health_html}
    {skips_html}
  </div>
</section>
"""


# ---------- section: CTA ----------

def _cta_section() -> str:
    return """
<section class="cta">
  <div>
    <h2>A public reading of <em>every</em> Federal Reserve voice.</h2>
    <p>Free, open, and refreshed weekday evenings. No login, no paywall, no newsletter. Bookmark the dashboard and check in when the committee speaks.</p>
  </div>
  <div class="cta__actions">
    <a class="btn cta__btn" href="#fomc">Open the dashboard →</a>
  </div>
</section>
"""


# ---------- section: footer ----------

def _footer_section(now: dt.datetime) -> str:
    return f"""
<footer>
  <div class="footer">
    <div class="footer__brand">
      <a class="logomark" href="#">
        <span class="logomark__bird"></span>
        <span>Fed Chirp</span>
      </a>
      <p class="footer__tagline">
        A personal monitor for Federal Reserve communications. Built quietly,
        run on a schedule, kept honest by an open rubric.
      </p>
    </div>
    <div>
      <h5>Coverage</h5>
      <ul>
        <li><a href="#speakers">Board governors · 7</a></li>
        <li><a href="#speakers">Regional presidents · 12</a></li>
        <li><a href="#fomc">FOMC statements</a></li>
        <li><a href="#fomc">FOMC minutes</a></li>
        <li><a href="#fomc">Press conferences</a></li>
      </ul>
    </div>
    <div>
      <h5>Read</h5>
      <ul>
        <li><a href="#method">The rubric</a></li>
        <li><a href="#method">Why score speeches?</a></li>
        <li><a href="#method">Sources &amp; scrapers</a></li>
        <li><a href="https://github.com/dstrunin/fed-chirp" target="_blank" rel="noopener">Code (GitHub)</a></li>
      </ul>
    </div>
    <div>
      <h5>Sections</h5>
      <ul>
        <li><a href="#fomc">FOMC pulse</a></li>
        <li><a href="#committee">Committee divergence</a></li>
        <li><a href="#speakers">Speakers</a></li>
        <li><a href="#recent">Recent</a></li>
        <li><a href="#method">Method</a></li>
      </ul>
    </div>
  </div>
  <div class="footer__bottom">
    <span>© {now.year} FED CHIRP · BUILT FOR ONE READER, USEFUL TO MORE</span>
    <span>NEXT SCAN · WEEKDAYS 18:30 ET</span>
  </div>
</footer>
"""


# ---------- _PAGE template (with embedded CSS) ----------

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Fed Chirp — Listening to the Federal Reserve</title>
<meta name="description" content="A daily reading of every Federal Reserve voice. Speeches, statements, minutes, and press conferences scored for hawkish or dovish tone."/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=IBM+Plex+Sans:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
:root {{
  --paper: #f3eee2;
  --paper-2: #ebe5d6;
  --paper-3: #e3dcc9;
  --ink: #1a1816;
  --ink-2: #2c2925;
  --mute: #6f6a60;
  --mute-2: #9c978c;
  --rule: #d6cfbe;
  --rule-2: #c8c0ad;
  --hawk: oklch(0.52 0.14 28);
  --hawk-soft: oklch(0.78 0.07 32);
  --hawk-bg: oklch(0.93 0.04 35);
  --dove: oklch(0.52 0.13 245);
  --dove-soft: oklch(0.78 0.06 245);
  --dove-bg: oklch(0.93 0.035 245);
  --neutral: #6f6a60;
  /* Market-data convention coloring used inside the Market Reaction
     section. Distinct from hawk/dove (which is policy-stance polarity)
     because for equity tickers a price-up move isn't inherently
     hawkish or dovish — it's just up. */
  --market-up: oklch(0.50 0.13 150);
  --market-down: oklch(0.52 0.16 25);
  --serif: 'Instrument Serif', 'EB Garamond', Georgia, serif;
  --sans: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif;
  --mono: 'JetBrains Mono', 'IBM Plex Mono', ui-monospace, monospace;
}}
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0; padding: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--sans);
  font-weight: 400; font-size: 15px; line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  font-feature-settings: "ss01", "ss02", "tnum";
}}
a {{ color: inherit; }}
.eyebrow {{
  font-family: var(--mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--mute);
  font-weight: 500;
}}
.t-hawk {{ color: var(--hawk); }}
.t-dove {{ color: var(--dove); }}
.t-neutral {{ color: var(--neutral); }}
.t-up {{ color: var(--market-up); }}
.t-down {{ color: var(--market-down); }}
.dot {{
  display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; vertical-align: middle; margin-right: 6px;
}}
.dot--hawkish, .dot--hawk {{ background: var(--hawk); }}
.dot--dovish, .dot--dove {{ background: var(--dove); }}
.dot--neutral {{ background: var(--neutral); }}
.btn {{
  font: inherit; font-family: var(--mono);
  font-size: 12px; font-weight: 500;
  letter-spacing: 0.04em; text-transform: uppercase;
  background: var(--ink); color: var(--paper);
  border: none; border-radius: 999px;
  padding: 11px 18px; cursor: pointer;
  text-decoration: none;
  display: inline-flex; align-items: center; gap: 8px;
  transition: transform .12s ease, background .12s ease;
}}
.btn:hover {{ transform: translateY(-1px); }}
.btn--ghost {{
  background: transparent; color: var(--ink);
  border: 1px solid var(--rule-2);
}}
.btn--ghost:hover {{ background: rgba(0,0,0,0.03); }}
.chip {{
  display: inline-flex; align-items: center; gap: 6px;
  font-family: var(--mono); font-size: 11px;
  letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--mute); padding: 4px 10px;
  border: 1px solid var(--rule); border-radius: 999px;
  background: transparent; cursor: pointer;
}}
.chip--active {{ border-color: var(--ink); color: var(--ink); }}

/* tonebar */
.tonebar {{
  position: relative;
  height: 8px;
  background: linear-gradient(90deg,
    var(--dove) 0%,
    color-mix(in oklch, var(--dove), var(--paper) 60%) 35%,
    var(--paper-3) 50%,
    color-mix(in oklch, var(--hawk), var(--paper) 60%) 65%,
    var(--hawk) 100%);
  border-radius: 999px;
  overflow: visible;
}}
.tonebar__tick {{
  position: absolute; top: -3px; width: 2px; height: 14px;
  background: var(--ink); transform: translateX(-1px);
}}
.tonebar__needle {{
  position: absolute; top: -6px; width: 14px; height: 20px;
  background: var(--ink); border-radius: 3px;
  transform: translateX(-7px);
  box-shadow: 0 2px 6px rgba(0,0,0,0.18);
}}

/* sparkline */
.spark {{ display: block; width: 100%; height: 36px; overflow: visible; }}
.spark__zero {{ stroke: var(--rule); stroke-width: 1; stroke-dasharray: 2 3; }}
.spark__line {{ fill: none; stroke: var(--ink); stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }}
.spark__dot {{ fill: var(--ink); }}
.spark--hawk .spark__line, .spark--hawk .spark__dot {{ stroke: var(--hawk); fill: var(--hawk); }}
.spark--dove .spark__line, .spark--dove .spark__dot {{ stroke: var(--dove); fill: var(--dove); }}

/* ticker */
@keyframes ticker-roll {{
  from {{ transform: translateX(0); }}
  to   {{ transform: translateX(-50%); }}
}}
.ticker {{
  overflow: hidden;
  border-top: 1px solid var(--rule-2);
  border-bottom: 1px solid var(--rule-2);
  background: var(--paper);
}}
.ticker__track {{
  display: flex; gap: 48px; padding: 12px 0;
  width: max-content;
  animation: ticker-roll 80s linear infinite;
}}
.ticker__item {{
  font-family: var(--mono); font-size: 12px;
  color: var(--ink);
  display: inline-flex; align-items: center; gap: 10px;
  white-space: nowrap;
}}
.ticker__item .label {{ color: var(--mute); }}
.ticker__sep {{ color: var(--rule-2); }}

/* logomark */
.logomark {{
  display: inline-flex; align-items: baseline; gap: 6px;
  font-family: var(--serif); font-size: 22px;
  letter-spacing: -0.01em; color: var(--ink);
  text-decoration: none;
}}
.logomark__bird {{
  display: inline-block; width: 10px; height: 10px;
  border-radius: 50%; background: var(--ink);
  position: relative; transform: translateY(-2px);
}}
.logomark__bird::after {{
  content: ""; position: absolute;
  right: -3px; top: 1px; width: 6px; height: 2px;
  background: var(--ink); transform: rotate(28deg); border-radius: 1px;
}}
.brand-pill {{
  font-family: var(--mono); font-size: 10px; color: var(--mute);
  letter-spacing: 0.1em; text-transform: uppercase;
  margin-left: 6px;
  border: 1px solid var(--rule-2);
  border-radius: 999px; padding: 2px 8px;
  align-self: center;
}}

/* topbar */
.topbar {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 22px 72px;
  border-bottom: 1px solid var(--rule);
  position: sticky; top: 0;
  background: rgba(243, 238, 226, 0.95);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  z-index: 20;
}}
.topbar__nav {{ display: flex; gap: 36px; align-items: center; }}
.topbar__nav a {{
  text-decoration: none; color: var(--ink-2);
  font-size: 13px; font-weight: 400;
  position: relative; padding-bottom: 4px;
}}
.topbar__nav a:hover {{ color: var(--ink); }}
.topbar__nav a.active::after,
.topbar__nav a:hover::after {{
  content: ""; position: absolute;
  bottom: -4px; left: 0; right: 0; height: 1px;
  background: var(--ink);
}}
.topbar__right {{ display: flex; gap: 14px; align-items: center; }}
.topbar__status {{
  font-family: var(--mono); font-size: 11px;
  color: var(--mute); letter-spacing: 0.04em;
  display: inline-flex; align-items: center;
}}
.pulse-dot {{
  width: 8px; height: 8px;
  background: var(--hawk); border-radius: 50%;
  display: inline-block; margin-right: 6px;
  box-shadow: 0 0 0 0 currentColor;
  animation: pulse 2.4s ease-in-out infinite;
  color: var(--hawk);
}}
@keyframes pulse {{
  0%, 100% {{ box-shadow: 0 0 0 0 currentColor; opacity: 1; }}
  50% {{ box-shadow: 0 0 0 6px transparent; opacity: 0.4; }}
}}
.menubtn {{
  display: none;
  width: 36px; height: 36px;
  border-radius: 50%;
  border: 1px solid var(--rule-2);
  background: transparent;
  align-items: center; justify-content: center;
  flex-direction: column; gap: 3px;
  cursor: pointer;
}}
.menubtn span {{ display: block; width: 14px; height: 1.5px; background: var(--ink); }}

/* hero */
.hero {{
  padding: 88px 72px 72px;
  display: grid;
  grid-template-columns: 1.05fr 1fr;
  gap: 72px;
  border-bottom: 1px solid var(--rule);
}}
.hero__head {{
  font-family: var(--serif);
  font-size: 96px; line-height: 0.92;
  letter-spacing: -0.025em;
  margin: 28px 0 28px;
  color: var(--ink);
}}
.hero__head em {{ font-style: italic; color: var(--ink); }}
.hero__sub {{
  max-width: 480px; color: var(--ink-2);
  font-size: 17px; line-height: 1.55;
}}
.hero__cta {{
  display: flex; gap: 12px;
  margin-top: 32px; align-items: center;
}}
.hero__meta {{
  margin-top: 56px;
  display: grid; grid-template-columns: repeat(3, 1fr);
  gap: 36px;
}}
.hero__metaitem .eyebrow {{ display: block; margin-bottom: 8px; }}
.hero__metaitem .v {{
  font-family: var(--serif);
  font-size: 38px; line-height: 1;
  letter-spacing: -0.02em;
}}
.hero__metaitem .vs {{ font-size: 13px; color: var(--mute); margin-top: 6px; }}
.hero__metaitem--small {{ font-size: 28px !important; }}

/* reading card */
.reading {{
  background: var(--paper-2);
  border: 1px solid var(--rule-2);
  border-radius: 4px;
  padding: 36px 36px 32px;
  position: relative;
}}
.reading__top {{
  display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 18px;
}}
.reading__big {{
  font-family: var(--serif);
  font-size: 200px; line-height: 0.85;
  letter-spacing: -0.045em;
  font-variant-numeric: tabular-nums;
  margin: 16px 0 12px;
}}
.reading__big .sign {{
  color: var(--mute-2); font-size: 0.8em;
  vertical-align: top; margin-right: 4px;
}}
.reading__label {{
  font-family: var(--serif); font-style: italic;
  font-size: 30px; color: var(--ink-2);
  line-height: 1.2; margin-bottom: 26px;
}}
.reading__bar {{ margin: 22px 0 28px; }}
.reading__barscale {{
  display: flex; justify-content: space-between;
  font-family: var(--mono); font-size: 10.5px;
  color: var(--mute); margin-top: 10px;
  letter-spacing: 0.08em;
}}
.reading__split {{
  display: grid; grid-template-columns: 1fr 1fr; gap: 28px;
  padding-top: 22px;
  border-top: 1px solid var(--rule-2);
}}
.reading__split .eyebrow {{ display: block; margin-bottom: 6px; }}
.reading__split .val {{
  font-family: var(--serif);
  font-size: 28px; line-height: 1;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}}
.reading__split .vsub {{
  margin-top: 4px;
  font-family: var(--mono); font-size: 11px;
  color: var(--mute); letter-spacing: 0.04em;
}}
.reading__delta {{
  display: inline-flex; align-items: center; gap: 6px;
  font-family: var(--mono); font-size: 12px;
  margin-top: 8px;
}}
.reading__driftrow {{
  grid-column: span 2;
  padding-top: 18px;
  border-top: 1px solid var(--rule);
}}
.reading__driftval {{
  display: flex; align-items: baseline; gap: 12px;
  margin-top: 6px;
}}
.reading__driftnote {{
  font-family: var(--serif); font-style: italic;
  color: var(--ink-2); font-size: 17px;
}}

/* delta indicators */
.delta-up {{ color: var(--hawk); }}
.delta-down {{ color: var(--dove); }}
.delta-flat {{ color: var(--mute); }}
.arrow-up::before {{ content: "↑ "; }}
.arrow-down::before {{ content: "↓ "; }}
.arrow-flat::before {{ content: "→ "; }}

/* sections */
.section {{
  padding: 88px 72px;
  border-bottom: 1px solid var(--rule);
}}
.section--paper2 {{ background: var(--paper-2); }}
.section__head {{
  display: grid;
  grid-template-columns: 360px 1fr;
  gap: 56px;
  margin-bottom: 48px;
  align-items: end;
}}
.section__title {{
  font-family: var(--serif);
  font-size: 56px; line-height: 0.95;
  letter-spacing: -0.025em;
  color: var(--ink);
  margin: 0;
}}
.section__title em {{ font-style: italic; }}
.section__lede {{
  font-size: 16px; color: var(--ink-2);
  max-width: 620px; line-height: 1.55;
  margin: 0;
}}
.section__num {{
  font-family: var(--mono);
  font-size: 11px; letter-spacing: 0.16em;
  color: var(--mute); text-transform: uppercase;
  margin-bottom: 18px;
}}

/* FOMC pulse grid.
   Markup is rendered latest-first (so the stacked mobile layout reads
   newest-at-top with no extra work). On desktop we flip the row visually
   via flex-direction: row-reverse so the chronology runs oldest-left →
   newest-right, matching the trajectory chart below it. The flex
   container gives equal-width cells via `flex: 1`. */
.fomc-grid {{
  display: flex;
  flex-direction: row-reverse;
  border-top: 1px solid var(--rule-2);
  border-bottom: 1px solid var(--rule-2);
}}
.fomc-cell {{
  flex: 1;
  padding: 28px 24px 24px;
  border-right: 1px solid var(--rule);
  position: relative;
}}
/* With row-reverse, source-order :first-child renders at the far right;
   suppress its right border so we don't get a trailing line at the
   container's right edge. */
.fomc-cell:first-child {{ border-right: none; }}
.fomc-cell--latest {{ background: var(--paper-2); }}
.fomc-cell--latest .date {{ color: var(--ink); }}
.fomc-cell .date {{
  font-family: var(--mono); font-size: 12px; color: var(--mute);
  letter-spacing: 0.05em;
}}
.fomc-cell .cur {{
  font-family: var(--mono); font-size: 11px;
  color: var(--mute); margin-left: 6px;
}}
.fomc-cell .combined {{
  font-family: var(--serif);
  font-size: 64px; line-height: 1;
  letter-spacing: -0.03em;
  margin: 22px 0 4px;
}}
.fomc-cell .clabel {{
  font-family: var(--serif); font-style: italic;
  font-size: 18px; color: var(--ink-2);
  margin-bottom: 18px;
}}
.fomc-cell .stack {{
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px 16px;
  border-top: 1px solid var(--rule);
  padding-top: 14px; font-size: 12px;
}}
.fomc-cell .stack .k {{
  color: var(--mute); font-family: var(--mono);
  font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase;
}}
.fomc-cell .stack .v {{
  font-family: var(--mono); font-variant-numeric: tabular-nums;
}}

/* trajectory + aside */
.fomc-after {{
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 48px; margin-top: 48px;
}}
.traj {{
  border: 1px solid var(--rule-2);
  background: var(--paper);
  padding: 28px 32px 24px;
  border-radius: 4px;
}}
.traj__head {{
  display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 16px;
}}
.traj__phrase {{
  font-family: var(--serif); font-size: 30px;
  letter-spacing: -0.02em; margin-top: 6px;
}}
.traj__phrase-sub {{
  font-family: var(--mono); font-size: 14px;
  color: var(--mute); margin-left: 8px; letter-spacing: 0;
}}
.traj__svg {{ width: 100%; height: 220px; overflow: visible; }}
.aside {{
  border: 1px solid var(--rule-2);
  background: var(--paper);
  border-radius: 4px;
  padding: 28px 32px;
}}
.aside__title {{
  font-family: var(--serif);
  font-size: 26px; letter-spacing: -0.015em;
  line-height: 1.15; margin: 10px 0 18px;
}}
.aside__list {{ list-style: none; padding: 0; margin: 0; }}
.aside__list li {{
  padding: 12px 0;
  border-top: 1px solid var(--rule);
  font-size: 13.5px; line-height: 1.5;
  color: var(--ink-2);
}}
.aside__list li strong {{ color: var(--ink); }}
.aside__list li em {{ color: var(--mute); font-style: italic; }}

/* committee strip */
.committee-strip {{
  display: grid;
  grid-template-columns: 1fr 1fr 1fr 1fr;
  gap: 48px;
  margin-bottom: 36px;
  padding: 24px 28px;
  background: var(--paper);
  border: 1px solid var(--rule-2);
  border-radius: 4px;
}}
.strip__big {{
  font-family: var(--serif); font-size: 42px;
  letter-spacing: -0.02em; line-height: 1;
  margin-top: 8px;
}}
.spread-change {{
  font-family: var(--mono); font-size: 13px;
  letter-spacing: 0; margin-left: 10px;
}}
.strip__pole {{
  font-family: var(--serif); font-size: 24px;
  letter-spacing: -0.01em; line-height: 1.1;
  margin-top: 10px;
}}
.strip__poletag {{
  font-family: var(--mono); font-size: 12px;
  color: var(--mute); letter-spacing: 0;
}}

/* camps */
.camps {{
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 1px;
  background: var(--rule);
  border: 1px solid var(--rule-2);
}}
.camp {{
  background: var(--paper);
  padding: 24px 24px 28px;
  min-height: 360px;
}}
.camp__head {{
  display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 18px; padding-bottom: 12px;
  border-bottom: 1px solid var(--rule);
}}
.camp__name {{
  font-family: var(--serif); font-size: 22px;
  letter-spacing: -0.01em;
}}
.camp__count {{
  font-family: var(--mono); font-size: 12px;
  color: var(--mute);
}}
.camp__row {{
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 0;
  border-bottom: 1px dotted var(--rule);
  font-size: 14px;
}}
.camp__row:last-child {{ border-bottom: 0; }}
.camp__row .pname {{ color: var(--ink); }}
.camp__row .role {{
  color: var(--mute); font-size: 11.5px;
  font-family: var(--mono); margin-left: 8px;
}}
.camp__row .pscore {{
  font-family: var(--mono); font-variant-numeric: tabular-nums;
  font-weight: 500;
}}
.camp--hawk .camp__name {{ color: var(--hawk); }}
.camp--dove .camp__name {{ color: var(--dove); }}
.camp__poles {{
  margin-top: 22px;
  padding-top: 16px;
  border-top: 1px solid var(--rule);
  font-family: var(--mono); font-size: 11px;
  color: var(--mute); letter-spacing: 0.04em;
}}
.camp__empty {{
  margin-top: 60px;
  text-align: center; color: var(--mute);
}}
.camp__empty-text {{
  font-family: var(--serif); font-style: italic;
  font-size: 22px; color: var(--ink-2);
  line-height: 1.3;
  max-width: 240px;
  margin: 0 auto;
}}
.camp__empty-sub {{
  font-family: var(--mono); font-size: 11px;
  letter-spacing: 0.06em; color: var(--mute);
  margin-top: 18px; text-transform: uppercase;
}}

/* speakers */
.chips-row {{
  display: flex; gap: 8px;
  margin-bottom: 24px; flex-wrap: wrap;
}}
.speakers-grid {{
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 1px;
  background: var(--rule);
  border: 1px solid var(--rule-2);
}}
.sp {{
  background: var(--paper);
  padding: 22px 22px 20px;
  min-height: 156px;
  display: flex; flex-direction: column;
  justify-content: space-between;
  cursor: pointer;
  transition: background 120ms;
}}
.sp:hover {{ background: var(--paper-2); }}
.sp__top {{
  display: flex; justify-content: space-between; align-items: flex-start;
}}
.sp__name {{
  font-family: var(--serif); font-size: 22px;
  line-height: 1.05; letter-spacing: -0.01em;
  max-width: 70%;
}}
.sp__region {{
  font-family: var(--mono); font-size: 10.5px;
  color: var(--mute); letter-spacing: 0.06em;
  text-transform: uppercase;
}}
.sp__role {{
  font-family: var(--mono); font-size: 11px;
  color: var(--mute); letter-spacing: 0.02em;
  margin-top: 4px;
}}
.sp__bot {{
  display: flex; align-items: end; justify-content: space-between;
  gap: 16px; margin-top: 18px;
}}
.sp__bot .v {{
  font-family: var(--serif); font-size: 32px;
  line-height: 1; letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}}
.sp__bot .vsub {{
  font-family: var(--mono); font-size: 10.5px; color: var(--mute);
  margin-top: 4px; letter-spacing: 0.04em;
}}
.sp__spark {{ flex: 1; max-width: 110px; }}

/* recent feed */
.feed {{
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 0 80px;
}}
.feed__row {{
  display: grid;
  grid-template-columns: 110px 1fr auto;
  gap: 24px;
  padding: 22px 0;
  border-bottom: 1px solid var(--rule);
  align-items: start;
}}
.feed__row:last-child {{ border-bottom: 0; }}
.feed__date {{
  font-family: var(--mono); font-size: 12px;
  color: var(--mute); letter-spacing: 0.04em;
  padding-top: 4px;
}}
.feed__name {{
  font-family: var(--serif); font-size: 22px;
  letter-spacing: -0.01em; line-height: 1.15;
  margin-bottom: 4px;
}}
.feed__title {{
  color: var(--ink-2); font-size: 14px;
  line-height: 1.4; margin-bottom: 8px;
}}
.feed__quote {{
  font-family: var(--serif); font-style: italic;
  font-size: 16px; color: var(--mute); line-height: 1.45;
  border-left: 1px solid var(--rule-2);
  padding-left: 12px; margin: 8px 0 0;
}}
.feed__score {{
  font-family: var(--serif); font-size: 32px; line-height: 1;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  text-align: right;
}}
.feed__scoresub {{
  font-family: var(--mono); font-size: 10.5px;
  color: var(--mute); letter-spacing: 0.04em;
  text-align: right; text-transform: uppercase;
}}

/* method */
.method-grid {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 32px;
}}
.method__card .step {{
  font-family: var(--mono);
  font-size: 11px; letter-spacing: 0.16em;
  color: var(--mute); text-transform: uppercase;
  margin-bottom: 12px;
}}
.method__card h4 {{
  font-family: var(--serif);
  font-size: 28px; letter-spacing: -0.015em;
  margin: 0 0 10px; line-height: 1.05;
}}
.method__card p {{
  color: var(--ink-2); font-size: 14px; line-height: 1.55;
  margin: 0;
}}

/* CTA */
.cta {{
  background: var(--ink);
  color: var(--paper);
  padding: 80px 72px;
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: 56px;
  align-items: center;
}}
.cta h2 {{
  font-family: var(--serif);
  font-size: 64px; line-height: 1;
  letter-spacing: -0.025em;
  margin: 0;
}}
.cta h2 em {{ color: color-mix(in oklch, var(--paper), var(--hawk-soft) 40%); }}
.cta p {{
  color: color-mix(in oklch, var(--paper), transparent 30%);
  margin: 16px 0 0;
  max-width: 480px;
  line-height: 1.5;
}}
.cta__actions {{
  display: flex; gap: 12px;
  align-items: center; justify-content: flex-end;
}}
.cta__btn {{
  background: var(--paper);
  color: var(--ink);
}}

/* footer */
.footer {{
  padding: 56px 72px 40px;
  display: grid;
  grid-template-columns: 1.4fr 1fr 1fr 1fr;
  gap: 48px;
}}
.footer h5 {{
  font-family: var(--mono); font-size: 11px;
  letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--mute);
  margin: 0 0 14px; font-weight: 500;
}}
.footer ul {{ list-style: none; padding: 0; margin: 0; }}
.footer ul li {{ padding: 4px 0; font-size: 14px; }}
.footer ul li a {{ text-decoration: none; }}
.footer ul li a:hover {{ text-decoration: underline; }}
.footer__brand .logomark {{ margin-bottom: 14px; }}
.footer__tagline {{
  color: var(--mute); font-size: 13px;
  line-height: 1.5; margin-top: 14px; max-width: 260px;
}}
.footer__bottom {{
  border-top: 1px solid var(--rule);
  padding: 24px 72px 40px;
  display: flex; justify-content: space-between;
  font-family: var(--mono); font-size: 11px;
  letter-spacing: 0.04em; color: var(--mute);
}}

/* ---------- FOMC market reactions ---------- */
.rxn-wrap {{
  border: 1px solid var(--rule-2);
  background: var(--paper);
  border-radius: 4px;
  padding: 8px 12px;
}}
table.rxn {{
  width: 100%;
  border-collapse: collapse;
  font-family: var(--mono);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}}
table.rxn th, table.rxn td {{
  text-align: right;
  padding: 10px 12px;
  border-bottom: 1px solid var(--rule);
}}
table.rxn tr:last-child td {{ border-bottom: 0; }}
table.rxn .rxn__meeting {{
  text-align: left;
  color: var(--ink);
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.02em;
}}
table.rxn .rxn__th {{
  font-weight: 500;
  font-size: 10.5px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--mute);
  padding-bottom: 8px;
}}
table.rxn .rxn__group {{
  text-align: center;
  border-bottom: 1px solid var(--rule-2);
  font-family: var(--mono);
  font-size: 10.5px;
  font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink);
  padding: 14px 12px 8px;
}}
table.rxn .rxn__time {{
  display: block;
  color: var(--mute);
  font-size: 9.5px;
  letter-spacing: 0.04em;
  margin-top: 2px;
  text-transform: none;
}}
table.rxn .rxn__divider {{ border-left: 1px solid var(--rule); }}
table.rxn td.bucket-cell {{ text-align: center; }}
.bucket-strong {{ background: var(--paper-3); font-weight: 600; }}
.bucket-mod {{ background: var(--paper-2); }}
.bucket-weak {{ color: var(--mute); }}
.bucket-zero {{ color: var(--mute-2); }}

/* ---------- Market-implied path ---------- */
.path__rates {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 28px;
  padding: 24px 28px;
  background: var(--paper);
  border: 1px solid var(--rule-2);
  border-radius: 4px;
  margin-bottom: 28px;
}}
.path__row .eyebrow {{ display: block; margin-bottom: 8px; }}
.path__rate {{
  font-family: var(--serif);
  font-size: 30px;
  letter-spacing: -0.02em;
  line-height: 1.15;
}}
.path__rate strong {{ font-weight: 400; }}
.path__delta {{
  display: block;
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0;
  margin-top: 4px;
}}
.path__sub {{
  font-family: var(--mono); font-size: 11px;
  color: var(--mute); margin-top: 4px;
}}
.path__gap {{
  background: var(--paper);
  border: 1px solid var(--rule-2);
  border-radius: 4px;
  padding: 22px 28px;
  margin-bottom: 28px;
  max-width: 720px;
}}
.path__gap .eyebrow {{ display: block; margin-bottom: 12px; }}
.path__gap-row {{
  display: flex; gap: 32px;
  margin-bottom: 14px;
}}
.path__gap-pair {{
  display: flex; flex-direction: column;
  gap: 4px;
  font-family: var(--mono); font-size: 11.5px;
  color: var(--mute); letter-spacing: 0.04em;
  text-transform: uppercase;
}}
.path__gap-pair > .t-hawk,
.path__gap-pair > .t-dove,
.path__gap-pair > .t-neutral {{
  font-family: var(--serif);
  font-size: 26px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
  text-transform: none;
}}
.path__gap-bottom {{
  display: flex;
  align-items: baseline;
  gap: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--rule);
}}
.path__gap-delta {{
  font-family: var(--serif);
  font-size: 30px;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}}
.path__gap-phrase {{
  font-family: var(--serif); font-style: italic;
  color: var(--ink-2); font-size: 16px;
}}
.path__h3 {{
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--mute);
  margin: 12px 0 14px;
  font-weight: 500;
}}

/* ---------- Transparency ---------- */
.trans-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 56px;
  padding-top: 12px;
  border-top: 1px solid var(--rule);
}}
.trans__col h3.trans__h3 {{
  font-family: var(--serif);
  font-size: 24px;
  letter-spacing: -0.015em;
  margin: 0 0 8px;
  font-weight: 400;
}}
.trans__lede {{
  font-size: 14px;
  color: var(--ink-2);
  line-height: 1.5;
  max-width: 480px;
  margin: 0 0 22px;
}}
.trans__list {{ display: flex; flex-direction: column; }}
.trans__row {{
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 0;
  border-bottom: 1px dotted var(--rule);
}}
.trans__row:last-child {{ border-bottom: 0; }}
.trans__name {{
  font-family: var(--serif); font-size: 16px;
  letter-spacing: -0.005em;
}}
.trans__sub {{
  font-family: var(--mono); font-size: 11px;
  color: var(--mute); letter-spacing: 0.04em;
  margin-top: 2px;
}}
.trans__right {{ text-align: right; }}
.trans__val {{
  font-family: var(--mono);
  font-size: 13px;
  color: var(--ink);
  font-variant-numeric: tabular-nums;
}}
.trans__group {{
  margin-bottom: 8px;
  border-bottom: 1px solid var(--rule);
  padding: 12px 0;
}}
.trans__group:last-child {{ border-bottom: 0; }}
.trans__group summary {{
  cursor: pointer;
  list-style: none;
  display: flex; justify-content: space-between; align-items: center;
}}
.trans__group summary::-webkit-details-marker {{ display: none; }}
.trans__group summary::after {{
  content: " ▾"; color: var(--mute); margin-left: 6px;
}}
.trans__group[open] summary::after {{ content: " ▴"; color: var(--ink); }}
.trans__reason {{
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--ink-2);
}}
.trans__count {{
  font-family: var(--serif);
  font-size: 22px;
  color: var(--ink);
}}
.trans__more {{
  font-family: var(--mono); font-size: 11px;
  color: var(--mute); margin-top: 8px; padding-left: 0;
}}
.trans__group .trans__list {{ margin-top: 12px; }}

/* ---------- mobile ---------- */
@media (max-width: 720px) {{
  .topbar {{
    padding: 14px 20px;
    position: sticky;
    top: 0;
  }}
  .topbar__nav, .topbar__right {{ display: none; }}
  .menubtn {{ display: flex; }}
  .topbar .logomark {{ font-size: 18px; }}
  .topbar .brand-pill {{ display: none; }}

  /* Hamburger drawer: slides under the topbar when toggled open. */
  .topbar__nav.topbar__nav--open {{
    display: flex;
    flex-direction: column;
    gap: 0;
    position: absolute;
    top: 100%;
    left: 0; right: 0;
    background: var(--paper);
    border-bottom: 1px solid var(--rule);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
    padding: 0;
    z-index: 19;
  }}
  .topbar__nav.topbar__nav--open a {{
    display: block;
    padding: 16px 20px;
    border-bottom: 1px solid var(--rule);
    font-size: 16px;
    color: var(--ink);
    text-decoration: none;
  }}
  .topbar__nav.topbar__nav--open a:last-child {{ border-bottom: 0; }}
  .topbar__nav.topbar__nav--open a::after {{ display: none; }}
  .topbar__nav.topbar__nav--open a:hover {{ background: var(--paper-2); }}

  /* Hamburger → X transition when drawer is open. */
  .menubtn span {{
    transition: transform 200ms ease, opacity 150ms ease;
    transform-origin: center;
  }}
  .menubtn--open span:nth-child(1) {{ transform: translateY(4.5px) rotate(45deg); }}
  .menubtn--open span:nth-child(2) {{ opacity: 0; }}
  .menubtn--open span:nth-child(3) {{ transform: translateY(-4.5px) rotate(-45deg); }}

  .ticker__track {{ animation-duration: 60s; gap: 28px; padding: 9px 0; }}
  .ticker__item, .ticker {{ font-size: 11px; }}

  .hero {{
    padding: 36px 20px 28px;
    grid-template-columns: 1fr;
    gap: 28px;
  }}
  .hero__head {{
    font-size: 56px; line-height: 0.95;
    margin: 16px 0 16px;
  }}
  .hero__sub {{ font-size: 14.5px; }}
  .hero__cta {{ flex-direction: column; align-items: stretch; }}
  .hero__cta .btn {{ justify-content: center; padding: 14px 18px; }}
  .hero__meta {{ grid-template-columns: 1fr 1fr; gap: 24px; margin-top: 36px; }}
  .hero__metaitem .v {{ font-size: 30px; }}

  .reading {{ padding: 22px 22px 20px; }}
  .reading__big {{ font-size: 110px; line-height: 0.85; }}
  .reading__label {{ font-size: 22px; }}
  .reading__split {{ gap: 16px; }}
  .reading__split .val {{ font-size: 24px; }}
  .reading__driftnote {{ font-size: 14px; }}

  .section {{ padding: 56px 20px; }}
  .section__head {{
    grid-template-columns: 1fr;
    gap: 14px;
    margin-bottom: 28px;
  }}
  .section__title {{ font-size: 38px; }}
  .section__lede {{ font-size: 14.5px; }}
  .section__num {{ margin-bottom: 12px; }}

  /* Mobile: column stack with newest at top. Markup is already
     latest-first, so plain `column` (not column-reverse) is correct. */
  .fomc-grid {{
    flex-direction: column;
    border-left: 0; border-right: 0;
  }}
  .fomc-cell {{
    border-right: none;
    border-bottom: 1px solid var(--rule);
    padding: 22px 0;
  }}
  .fomc-cell:first-child {{ border-right: none; }}  /* idempotent on mobile */
  .fomc-cell:last-child {{ border-bottom: none; }}
  .fomc-cell .combined {{ font-size: 56px; }}

  .fomc-after {{ grid-template-columns: 1fr; gap: 28px; margin-top: 28px; }}
  .traj {{ padding: 18px 16px 14px; }}
  .traj__phrase {{ font-size: 22px; }}
  .traj__svg {{ height: 160px; }}
  .aside {{ padding: 22px 20px; }}
  .aside__title {{ font-size: 22px; }}

  .committee-strip {{
    grid-template-columns: 1fr 1fr;
    gap: 18px;
    padding: 18px 18px;
  }}
  .strip__big {{ font-size: 28px; }}
  .strip__pole {{ font-size: 18px; }}
  .camps {{
    grid-template-columns: 1fr;
    gap: 1px;
  }}
  .camp {{ min-height: 0; padding: 22px 20px; }}

  .chips-row {{
    flex-wrap: nowrap;
    overflow-x: auto;
    margin: 0 -20px 22px;
    padding: 0 20px 4px;
    scrollbar-width: none;
  }}
  .chips-row::-webkit-scrollbar {{ display: none; }}
  .chips-row .chip {{ flex: 0 0 auto; }}

  .speakers-grid {{
    grid-template-columns: 1fr;
    border-left: 0; border-right: 0;
  }}
  .sp {{
    min-height: 0;
    padding: 18px 18px;
    border-bottom: 1px solid var(--rule);
  }}
  .sp:last-child {{ border-bottom: none; }}
  .sp__name {{ font-size: 18px; }}
  .sp__bot .v {{ font-size: 22px; }}

  .feed {{ grid-template-columns: 1fr; gap: 0; }}
  .feed__row {{
    grid-template-columns: 1fr;
    gap: 8px;
    padding: 18px 0;
  }}
  .feed__date {{ padding-top: 0; font-size: 11px; }}
  .feed__name {{ font-size: 19px; }}
  .feed__score {{ text-align: left; font-size: 26px; }}
  .feed__scoresub {{ text-align: left; }}

  .method-grid {{
    grid-template-columns: 1fr;
    gap: 22px;
  }}
  .method__card h4 {{ font-size: 24px; }}

  .cta {{
    padding: 56px 20px;
    grid-template-columns: 1fr;
    gap: 28px;
  }}
  .cta h2 {{ font-size: 38px; }}
  .cta__actions {{ justify-content: flex-start; }}

  .footer {{
    padding: 36px 20px 24px;
    grid-template-columns: 1fr 1fr;
    gap: 28px;
  }}
  .footer__brand {{ grid-column: span 2; }}
  .footer__bottom {{
    padding: 18px 20px 32px;
    flex-direction: column;
    gap: 6px;
  }}

  /* Reactions table → cards on mobile (uses .cards class behavior) */
  table.rxn thead {{ display: none; }}
  table.rxn, table.rxn tbody, table.rxn tr, table.rxn td {{ display: block; }}
  table.rxn tr {{
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 10px 14px;
    margin: 0 0 12px;
    background: var(--paper);
  }}
  table.rxn td {{
    border: none;
    text-align: left;
    padding: 4px 0;
    font-size: 12.5px;
  }}
  table.rxn td::before {{
    content: attr(data-label) ":";
    display: inline-block;
    min-width: 110px;
    color: var(--mute);
    font-size: 10.5px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin-right: 8px;
  }}
  table.rxn td.rxn__divider {{ border-left: none; }}
  table.rxn td.rxn__meeting {{ font-size: 14px; padding-bottom: 8px; }}
  table.rxn td.rxn__meeting::before {{ display: none; }}
  table.rxn td.bucket-cell {{ text-align: left; }}

  /* Market path */
  .path__rates {{
    grid-template-columns: 1fr;
    gap: 18px;
    padding: 18px 18px;
  }}
  .path__rate {{ font-size: 24px; }}
  .path__gap {{ padding: 18px 18px; }}
  .path__gap-row {{ flex-direction: column; gap: 12px; }}
  .path__gap-pair > .t-hawk,
  .path__gap-pair > .t-dove,
  .path__gap-pair > .t-neutral {{ font-size: 22px; }}
  .path__gap-delta {{ font-size: 26px; }}
  .path__gap-phrase {{ font-size: 14px; }}

  /* Transparency */
  .trans-grid {{
    grid-template-columns: 1fr;
    gap: 32px;
    padding-top: 8px;
  }}
  .trans__col h3.trans__h3 {{ font-size: 20px; }}
  .trans__lede {{ font-size: 13.5px; }}
  .trans__count {{ font-size: 18px; }}
}}

@media (prefers-reduced-motion: reduce) {{
  .ticker__track {{ animation: none; }}
  .pulse-dot {{ animation: none; }}
  .btn:hover {{ transform: none; }}
}}
</style>
</head>
<body>
{topbar}
{ticker}
{hero}
{pulse}
{reactions}
{market_path}
{committee}
{speakers}
{recent}
{method}
{transparency}
{cta}
{footer}
<script>
(function () {{
  // Region tab filter (client-side)
  var chips = document.querySelectorAll('.chips-row .chip');
  var cards = document.querySelectorAll('.speakers-grid .sp');
  function applyFilter(region) {{
    cards.forEach(function (c) {{
      var match = (region === 'all') || c.getAttribute('data-region') === region;
      c.style.display = match ? '' : 'none';
    }});
  }}
  chips.forEach(function (chip) {{
    chip.addEventListener('click', function () {{
      chips.forEach(function (c) {{ c.classList.remove('chip--active'); }});
      chip.classList.add('chip--active');
      applyFilter(chip.getAttribute('data-region') || 'all');
    }});
  }});

  // Mobile hamburger menu — toggles the topbar nav as a drawer.
  var menubtn = document.querySelector('.menubtn');
  var topnav = document.querySelector('.topbar__nav');
  if (menubtn && topnav) {{
    var setOpen = function (open) {{
      topnav.classList.toggle('topbar__nav--open', open);
      menubtn.classList.toggle('menubtn--open', open);
      menubtn.setAttribute('aria-expanded', String(open));
    }};
    setOpen(false);
    menubtn.addEventListener('click', function (e) {{
      e.preventDefault();
      var open = !topnav.classList.contains('topbar__nav--open');
      setOpen(open);
    }});
    // Close drawer when any nav link is tapped.
    topnav.querySelectorAll('a').forEach(function (a) {{
      a.addEventListener('click', function () {{ setOpen(false); }});
    }});
    // Close drawer if the user resizes back to desktop width.
    window.addEventListener('resize', function () {{
      if (window.innerWidth > 720) setOpen(false);
    }});
  }}

  // Reformat the regen timestamp into the visitor's local timezone.
  var t = document.getElementById('regen-time');
  if (t) {{
    var d = new Date(t.getAttribute('datetime'));
    if (!isNaN(d)) {{
      var opts = {{ year: 'numeric', month: 'short', day: 'numeric',
                   hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }};
      try {{
        t.textContent = new Intl.DateTimeFormat(undefined, opts).format(d);
      }} catch (e) {{}}
    }}
  }}
}})();
</script>
</body>
</html>
"""
