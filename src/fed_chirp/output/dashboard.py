"""Render a self-contained HTML dashboard.

Layout:
  - Header summary: total speeches, latest scan time
  - Per-speaker row: name, role, current 90-day mean, sparkline (inline SVG),
    most recent score
  - Recent speeches table: date, speaker, score, label, title, link
"""

from __future__ import annotations

import datetime as dt
import html
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..analysis.deltas import WINDOW_DAYS
from ..fetchers.federalreserve import Speaker
from ..fetchers.fomc import (
    DOC_MINUTES,
    DOC_PRESSER,
    DOC_STATEMENT,
    FOMC_SPEAKER_KEY,
    doc_type_label,
)
from ..storage.db import StoredScore

SPARK_W = 220
SPARK_H = 36
SPARK_PADDING = 4

HAWK_COLOR = "#c0392b"
DOVE_COLOR = "#2e86c1"
NEUTRAL_COLOR = "#7f8c8d"


def render(
    speakers: list[Speaker],
    scores: list[StoredScore],
    out_path: Path,
) -> None:
    speech_scores = [s for s in scores if s.doc_type == "speech"]
    fomc_scores = [s for s in scores if s.doc_type != "speech"]

    by_speaker: dict[str, list[StoredScore]] = defaultdict(list)
    for s in speech_scores:
        by_speaker[s.speaker_key].append(s)
    for k in by_speaker:
        by_speaker[k].sort(key=lambda x: x.speech_date)

    governor_speakers = [sp for sp in speakers if sp.key != FOMC_SPEAKER_KEY]

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    rows_html = "\n".join(
        _speaker_row(sp, by_speaker.get(sp.key, [])) for sp in governor_speakers
    )
    meetings = _group_by_meeting(fomc_scores)
    meetings_html = _fomc_meetings_section(meetings)
    fomc_html = _fomc_pulse(fomc_scores)
    recent_html = _recent_table(speakers, speech_scores[:30])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _PAGE.format(
            now=now,
            total=len(scores),
            meetings_html=meetings_html,
            fomc_html=fomc_html,
            speakers_html=rows_html,
            recent_html=recent_html,
        ),
        encoding="utf-8",
    )


def _speaker_row(speaker: Speaker, scores: list[StoredScore]) -> str:
    if not scores:
        return f"""
        <tr>
          <td class="name">{html.escape(speaker.name)}</td>
          <td class="role">{html.escape(speaker.role)}</td>
          <td class="muted">no speeches scored yet</td>
          <td></td>
          <td></td>
        </tr>"""

    today = dt.date.today()
    cutoff = today - dt.timedelta(days=WINDOW_DAYS)
    window = [s for s in scores if s.speech_date >= cutoff]
    avg_90 = (sum(s.score for s in window) / len(window)) if window else float("nan")

    last = scores[-1]
    spark = _sparkline([s.score for s in scores[-30:]])
    n_total = len(scores)

    avg_str = f"{avg_90:+.2f}" if not _isnan(avg_90) else "—"
    avg_class = _polarity_class(avg_90 if not _isnan(avg_90) else 0)
    last_class = _polarity_class(last.score)

    return f"""
        <tr>
          <td class="name">{html.escape(speaker.name)}</td>
          <td class="role">{html.escape(speaker.role)}</td>
          <td class="score {avg_class}">{avg_str}<span class="muted"> (90d, n={len(window)})</span></td>
          <td class="spark">{spark}</td>
          <td class="last">
            <span class="score {last_class}">{last.score:+.2f}</span>
            <span class="muted">{last.speech_date.isoformat()} ({n_total} total)</span>
          </td>
        </tr>"""


@dataclass
class _Meeting:
    """A single FOMC meeting, grouping all docs that belong to it.
    Statements/pressers happen on the meeting decision day; minutes
    are released ~3 weeks later but reference the meeting they cover."""
    meeting_date: dt.date
    statement: StoredScore | None = None
    presser: StoredScore | None = None
    minutes: StoredScore | None = None

    @property
    def combined(self) -> float | None:
        """Combined statement+presser score (the meeting's same-day tone).
        Equal weight when both exist; otherwise whichever single score is
        present. None when neither has been scored yet."""
        parts = [s.score for s in (self.statement, self.presser) if s is not None]
        return sum(parts) / len(parts) if parts else None

    @property
    def drift(self) -> float | None:
        """Intra-meeting drift: presser score minus statement score.
        Positive = Q&A pulled hawk against the prepared text. None if
        either is missing."""
        if self.statement is None or self.presser is None:
            return None
        return self.presser.score - self.statement.score


# Title format: "Minutes of the Federal Open Market Committee, March 17–18, 2026"
# We pull the LAST day-number and the year out, plus the month name.
_MEETING_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}(?:[–—\-]+(\d{1,2}))?,\s+(\d{4})"
)
_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 1)}


def _meeting_date_for(score: StoredScore) -> dt.date | None:
    """Extract the FOMC meeting date that a given doc belongs to.

    - Statements + pressers happen on the meeting day, so meeting_date
      equals the doc's speech_date.
    - Minutes are released later; the meeting date lives in the title.
      Returns None if the title doesn't parse (we then drop the doc from
      the meeting view rather than guess).
    """
    if score.doc_type != "fomc_minutes":
        return score.speech_date
    # Look for the title on this score; we kept it on StoredSpeech, not StoredScore,
    # so we lean on the URL + score_date pattern when we can. But StoredScore
    # carries the rationale string only — we need title from elsewhere.
    # Workaround: callers will pass title in via _group_by_meeting().
    return None


def _group_by_meeting(fomc_scores: list[StoredScore]) -> list[_Meeting]:
    """Group FOMC docs into meetings keyed on meeting_date.

    For statements/pressers, meeting_date == speech_date. For minutes,
    we parse the meeting date out of the title; if parsing fails we fall
    back to the release date so the doc still shows up (just not paired
    with its corresponding statement+presser).
    """
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

    meetings = list(by_date.values())
    meetings.sort(key=lambda m: m.meeting_date, reverse=True)
    return meetings


def _parse_minutes_meeting_date(title: str) -> dt.date | None:
    m = _MEETING_DATE_RE.search(title)
    if not m:
        return None
    month_name, day_end_opt, year = m.group(1), m.group(2), int(m.group(3))
    # The full match also captures the start day; we want the END day of
    # the meeting (last day of deliberations), which is when policy is set.
    full = m.group(0)
    nums = re.findall(r"\d+", full)
    if not nums:
        return None
    day = int(nums[-2]) if len(nums) >= 2 else int(nums[-1])  # second-to-last is end-day, last is year
    # Heuristic check: pick the day that's <= 31
    cand_days = [int(n) for n in nums if int(n) <= 31]
    if cand_days:
        day = cand_days[-1]
    try:
        return dt.date(year, _MONTHS[month_name], day)
    except (KeyError, ValueError):
        return None


def _fomc_meetings_section(meetings: list[_Meeting]) -> str:
    """Render the per-meeting combined view: sparkline of combined scores
    across meetings, plus a table with statement/presser/combined/drift/
    minutes columns and meeting-over-meeting delta."""
    if not meetings:
        return ""

    # Sparkline: oldest to newest, combined-score series (skip meetings
    # where neither statement nor presser is scored).
    series = [
        m.combined for m in reversed(meetings) if m.combined is not None
    ]
    spark = _sparkline(series) if series else ""

    rows: list[str] = []
    for i, m in enumerate(meetings):
        prior = next(
            (p for p in meetings[i + 1 :] if p.combined is not None),
            None,
        )
        meeting_delta = (
            (m.combined - prior.combined)
            if (m.combined is not None and prior is not None)
            else None
        )

        def _cell(score_obj: StoredScore | None) -> str:
            if score_obj is None:
                return '<span class="muted">—</span>'
            cls = _polarity_class(score_obj.score)
            return f'<span class="score {cls}">{score_obj.score:+.2f}</span>'

        if m.combined is None:
            combined_html = '<span class="muted">—</span>'
        else:
            cls = _polarity_class(m.combined)
            combined_html = f'<span class="score {cls}">{m.combined:+.2f}</span>'

        if m.drift is None:
            drift_html = '<span class="muted">—</span>'
        else:
            d_cls = (
                _polarity_class(m.drift * 2) if abs(m.drift) >= 0.3 else "neutral"
            )
            drift_html = f'<span class="score {d_cls}">{m.drift:+.2f}</span>'

        if meeting_delta is None:
            delta_html = '<span class="muted">— (first)</span>'
        else:
            d_cls = (
                _polarity_class(meeting_delta * 2)
                if abs(meeting_delta) >= 0.3
                else "neutral"
            )
            delta_html = f'<span class="score {d_cls}">{meeting_delta:+.2f}</span>'

        if m.minutes is not None:
            mcls = _polarity_class(m.minutes.score)
            mins_html = (
                f'<span class="score {mcls}">{m.minutes.score:+.2f}</span>'
                f'<span class="muted"> ({m.minutes.speech_date.isoformat()})</span>'
            )
        else:
            mins_html = '<span class="muted">pending</span>'

        rows.append(
            f"""<tr>
              <td>{m.meeting_date.isoformat()}</td>
              <td>{_cell(m.statement)}</td>
              <td>{_cell(m.presser)}</td>
              <td>{combined_html}</td>
              <td>{drift_html}</td>
              <td>{delta_html}</td>
              <td>{mins_html}</td>
            </tr>"""
        )

    spark_html = (
        f'<p class="meta">Combined-score trajectory (oldest → newest): {spark}</p>'
        if spark
        else ""
    )

    return f"""
        <h3>Per-meeting combined view</h3>
        {spark_html}
        <table class="fomc">
          <thead><tr>
            <th>Meeting</th>
            <th>Statement</th>
            <th>Presser</th>
            <th>Combined</th>
            <th>Drift (P−S)</th>
            <th>Δ vs prior meeting</th>
            <th>Minutes</th>
          </tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
        <p class="meta">
          <strong>Combined</strong> = average of statement and presser scores
          (same-day tone). <strong>Drift</strong> = presser − statement: positive
          means Powell's Q&amp;A pulled hawkish against the prepared text.
          <strong>Δ vs prior meeting</strong> compares this meeting's combined
          score to the previous meeting's.
        </p>"""


def _fomc_pulse(fomc_scores: list[StoredScore]) -> str:
    """Render the FOMC pulse section: 4 most recent of each doc_type with
    score, delta-vs-prior, and (for statements) auto-generated diff notes."""
    if not fomc_scores:
        return '<p class="muted">No FOMC documents scored yet.</p>'

    sections: list[str] = []
    for doc_type in (DOC_STATEMENT, DOC_PRESSER, DOC_MINUTES):
        of_type = [s for s in fomc_scores if s.doc_type == doc_type]
        of_type.sort(key=lambda x: x.speech_date, reverse=True)
        if not of_type:
            continue
        rows: list[str] = []
        for i, s in enumerate(of_type[:4]):
            prior = of_type[i + 1] if i + 1 < len(of_type) else None
            delta = (s.score - prior.score) if prior else None
            cls = _polarity_class(s.score)
            if delta is None:
                delta_html = '<span class="muted">— (first)</span>'
            else:
                d_cls = _polarity_class(delta * 2) if abs(delta) >= 0.3 else "neutral"
                delta_html = f'<span class="score {d_cls}">{delta:+.2f}</span>'

            notes_html = ""
            if doc_type == DOC_STATEMENT and s.diff_notes:
                items = "".join(
                    f"<li>{_render_inline_md(n)}</li>" for n in s.diff_notes
                )
                notes_html = (
                    f'<tr class="notes-row"><td colspan="5">'
                    f'<details><summary>What changed vs prior statement</summary>'
                    f'<ul class="diff-notes">{items}</ul>'
                    f'</details></td></tr>'
                )

            rows.append(
                f"""<tr>
                  <td>{s.speech_date.isoformat()}</td>
                  <td class="score {cls}">{s.score:+.2f}</td>
                  <td>{html.escape(s.label)}</td>
                  <td>{delta_html}</td>
                  <td><a href="{html.escape(s.speech_url)}" target="_blank" rel="noopener">link</a></td>
                </tr>{notes_html}"""
            )
        sections.append(f"""
        <h3>{html.escape(doc_type_label(doc_type))}</h3>
        <table class="fomc">
          <thead><tr><th>Date</th><th>Score</th><th>Label</th><th>Δ vs prior</th><th>URL</th></tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>""")
    return "\n".join(sections)


def _render_inline_md(text: str) -> str:
    """Tiny markdown subset: **bold** and *italic*. HTML-escaped first."""
    import re
    safe = html.escape(text)
    safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", safe)
    return safe


def _recent_table(speakers: list[Speaker], scores: list[StoredScore]) -> str:
    by_key = {sp.key: sp for sp in speakers}
    if not scores:
        return '<p class="muted">No speeches scored yet.</p>'
    rows = []
    for s in scores:
        sp = by_key.get(s.speaker_key)
        speaker_name = sp.name if sp else s.speaker_key
        cls = _polarity_class(s.score)
        rationale = html.escape(s.rationale)
        rows.append(
            f"""<tr>
              <td>{s.speech_date.isoformat()}</td>
              <td>{html.escape(speaker_name)}</td>
              <td class="score {cls}">{s.score:+.2f}</td>
              <td>{html.escape(s.label)}</td>
              <td><a href="{html.escape(s.speech_url)}" target="_blank" rel="noopener">link</a></td>
              <td class="rationale">{rationale}</td>
            </tr>"""
        )
    return f"""
    <table class="recent">
      <thead>
        <tr><th>Date</th><th>Speaker</th><th>Score</th><th>Label</th><th>URL</th><th>Rationale</th></tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>"""


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        values = [values[0], values[0]]

    lo, hi = -2.0, 2.0
    w = SPARK_W - 2 * SPARK_PADDING
    h = SPARK_H - 2 * SPARK_PADDING

    def x(i: int) -> float:
        if len(values) == 1:
            return SPARK_PADDING + w / 2
        return SPARK_PADDING + (i / (len(values) - 1)) * w

    def y(v: float) -> float:
        clamped = max(lo, min(hi, v))
        return SPARK_PADDING + (1 - (clamped - lo) / (hi - lo)) * h

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    zero_y = y(0.0)
    return (
        f'<svg viewBox="0 0 {SPARK_W} {SPARK_H}" width="{SPARK_W}" height="{SPARK_H}">'
        f'<line x1="{SPARK_PADDING}" y1="{zero_y:.1f}" x2="{SPARK_W - SPARK_PADDING}" '
        f'y2="{zero_y:.1f}" stroke="#ddd" stroke-width="1"/>'
        f'<polyline fill="none" stroke="#333" stroke-width="1.5" points="{pts}"/>'
        f"</svg>"
    )


def _polarity_class(score: float) -> str:
    if score > 0.3:
        return "hawk"
    if score < -0.3:
        return "dove"
    return "neutral"


def _isnan(x: float) -> bool:
    return x != x


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Fed Chirp</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 2rem auto; max-width: 1100px; padding: 0 1rem; color: #222; }}
  h1 {{ margin-bottom: 0.2rem; }}
  .meta {{ color: #777; font-size: 0.9rem; margin-bottom: 1.5rem; }}
  table {{ width: 100%; border-collapse: collapse; margin: 1rem 0 2rem; }}
  th, td {{ padding: 0.5rem 0.6rem; text-align: left; border-bottom: 1px solid #eee;
           vertical-align: top; font-size: 0.92rem; }}
  th {{ background: #fafafa; font-weight: 600; color: #555; }}
  td.name {{ font-weight: 600; }}
  td.role {{ color: #666; }}
  td.spark {{ width: """ + str(SPARK_W) + """px; }}
  td.score, span.score {{ font-variant-numeric: tabular-nums; font-weight: 600; }}
  .hawk {{ color: """ + HAWK_COLOR + """; }}
  .dove {{ color: """ + DOVE_COLOR + """; }}
  .neutral {{ color: """ + NEUTRAL_COLOR + """; }}
  .muted {{ color: #999; font-weight: normal; font-size: 0.85em; }}
  table.recent td.rationale {{ max-width: 380px; color: #555; }}
  table.recent td a, table.fomc td a {{ color: #1f6feb; }}
  table.fomc {{ margin-bottom: 1.5rem; }}
  table.fomc tr.notes-row td {{ background: #fafbfc; padding: 0.4rem 0.8rem 0.7rem; border-bottom: 1px solid #eee; }}
  details {{ font-size: 0.9rem; color: #444; }}
  details summary {{ cursor: pointer; color: #1f6feb; padding: 0.2rem 0; user-select: none; }}
  ul.diff-notes {{ margin: 0.4rem 0 0.2rem; padding-left: 1.2rem; }}
  ul.diff-notes li {{ margin: 0.3rem 0; line-height: 1.45; }}
  h3 {{ font-size: 1rem; color: #444; margin: 1rem 0 0.4rem; }}
  .legend {{ font-size: 0.85rem; color: #666; margin-bottom: 1.5rem; }}
  .legend span {{ font-weight: 600; }}
</style>
</head>
<body>
<h1>Fed Chirp</h1>
<div class="meta">Last regenerated {now} &middot; {total} speeches scored</div>
<div class="legend">
  Scale: <span class="dove">−2 dovish</span> &middot;
  <span class="neutral">0 neutral</span> &middot;
  <span class="hawk">+2 hawkish</span>
</div>

<h2>FOMC pulse</h2>
{meetings_html}
{fomc_html}

<h2>Board governors</h2>
<table>
  <thead><tr><th>Speaker</th><th>Role</th><th>90d mean</th>
             <th>Last 30 speeches</th><th>Most recent</th></tr></thead>
  <tbody>{speakers_html}</tbody>
</table>

<h2>Recent speeches</h2>
{recent_html}

</body>
</html>
"""
