"""Coverage health: detect speakers whose feeds may have gone silent.

A long quiet period can mean either (a) the scraper broke / RSS feed changed,
or (b) the speaker genuinely hasn't published a transcript-archived speech
(e.g., Atlanta Fed's Bostic, who appears mostly on YouTube/external media
that we can't ingest). We can't tell those apart automatically — we just
flag the gap so a human can investigate.

Keyed on the `speeches` table (any doc_type, any score), not on
`speech_scores`. A regulatory governor whose recent speeches are all
correctly excluded by the rubric should NOT show as stale here — the
scraper is working, they're just on a non-MP beat.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from ..fetchers.federalreserve import Speaker

DEFAULT_THRESHOLD_DAYS = 60


@dataclass(frozen=True)
class StaleSpeaker:
    speaker: Speaker
    last_speech_date: dt.date | None  # None if no speeches ever stored
    days_silent: int  # measured from `asof`; large sentinel when last_speech_date is None


def find_stale(
    speakers: list[Speaker],
    last_dates: dict[str, dt.date],
    asof: dt.date,
    threshold_days: int = DEFAULT_THRESHOLD_DAYS,
) -> list[StaleSpeaker]:
    """Return speakers whose latest speech is older than `threshold_days`.

    `last_dates` is a {speaker_key: latest_speech_date} mapping (e.g. from
    `db.last_speech_dates()`). Speakers with no entry are reported with
    `last_speech_date=None` and `days_silent=10**6` (sentinel) so they sort
    to the top of any "most stale first" view.
    Sorted descending by days_silent.
    """
    stale: list[StaleSpeaker] = []
    for sp in speakers:
        last = last_dates.get(sp.key)
        if last is None:
            stale.append(StaleSpeaker(sp, None, 10**6))
            continue
        gap = (asof - last).days
        if gap > threshold_days:
            stale.append(StaleSpeaker(sp, last, gap))
    stale.sort(key=lambda s: s.days_silent, reverse=True)
    return stale


# A scan at 18:30 ET allows four hours after the statement and three and a
# half hours after the presser starts for transcript publication/ingestion.
# Already discovered/stored documents are checked immediately, without grace.
FOMC_DEADLINE_ET = dt.time(18)


@dataclass(frozen=True)
class FomcHealth:
    latest_expected: dt.date | None
    issues: tuple[str, ...]
    incomplete_dates: tuple[dt.date, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues


def check_fomc(db, refs=(), *, now: dt.datetime | None = None,
               discovery_error: str | None = None) -> FomcHealth:
    """Read-only completeness check using calendar, stored bodies and scores.

    Calendar expectations apply only to the latest meeting past 18:00 New
    York time, honoring has_press_conf. All discovered/stored FOMC documents
    (including minutes) must be scored; ordinary speeches never participate.
    Matching by (type, date) accepts canonicalized/deduplicated URLs. An empty
    or exhausted calendar is unknown, not a clean bill of health. No network
    or scoring calls are made here. Pass a Path to open an existing database
    read-only (no migrations or accidental creation), or a Database instance.
    """
    from zoneinfo import ZoneInfo
    from ..fetchers.fomc import DOC_STATEMENT, DOC_PRESSER, DOC_MINUTES, doc_type_label

    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("FOMC health requires a timezone-aware now")
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    connection = (closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True))
                  if isinstance(db, Path) else db.connect())
    with connection as conn:
        conn.row_factory = sqlite3.Row
        meetings = conn.execute(
            "SELECT meeting_date, has_press_conf FROM fomc_meetings ORDER BY meeting_date"
        ).fetchall()
        rows = conn.execute(
            """SELECT s.url, s.doc_type, s.speech_date, sc.score
               FROM speeches s LEFT JOIN speech_scores sc ON sc.speech_url = s.url
               WHERE s.doc_type IN ('fomc_statement', 'fomc_presser', 'fomc_minutes')"""
        ).fetchall()

    issues: list[str] = []
    if discovery_error:
        issues.append(f"FOMC discovery failed: {discovery_error}")
    if not meetings:
        issues.append("FOMC calendar unavailable; latest completed meeting cannot be verified")
    elif dt.date.fromisoformat(meetings[-1]["meeting_date"]) < eastern.date():
        issues.append("FOMC calendar exhausted; refresh required to verify latest completed meeting")

    completed = [r for r in meetings if dt.datetime.combine(
        dt.date.fromisoformat(r["meeting_date"]), FOMC_DEADLINE_ET,
        tzinfo=eastern.tzinfo,
    ) <= eastern]
    latest = dt.date.fromisoformat(completed[-1]["meeting_date"]) if completed else None
    # Each candidate is identified by type + publication date, not by URL.
    expected: dict[tuple[str, dt.date], set[str]] = {}
    stored: set[tuple[str, dt.date]] = set()
    scored: set[tuple[str, dt.date]] = set()
    for row in rows:
        key = (row["doc_type"], dt.date.fromisoformat(row["speech_date"]))
        expected.setdefault(key, set()).add(row["url"])
        stored.add(key)
        if row["score"] is not None:
            scored.add(key)
    for ref in refs:
        if ref.doc_type not in (DOC_STATEMENT, DOC_PRESSER, DOC_MINUTES):
            continue
        expected.setdefault((ref.doc_type, ref.pub_date), set()).add(ref.url)
    if latest:
        expected.setdefault((DOC_STATEMENT, latest), set())
        if completed[-1]["has_press_conf"]:
            expected.setdefault((DOC_PRESSER, latest), set())
    # A newly discovered/stored decision can be newer than the scheduled one
    # (e.g. an unscheduled meeting, or discovery before the evening deadline).
    decision_dates = [date for kind, date in expected if kind in (DOC_STATEMENT, DOC_PRESSER)]
    latest = max(decision_dates, default=latest) if latest is None else max([latest, *decision_dates])
    incomplete: set[dt.date] = set()
    for (kind, date), urls in sorted(expected.items(), key=lambda item: (item[0][1], item[0][0])):
        key = (kind, date)
        if key in scored:
            continue
        status = "unscored" if key in stored else ("not stored" if urls else "missing")
        suffix = f" ({', '.join(sorted(urls))})" if urls else ""
        issues.append(f"{date.isoformat()} {doc_type_label(kind)}: {status}{suffix}")
        if kind in (DOC_STATEMENT, DOC_PRESSER):
            incomplete.add(date)
    return FomcHealth(latest, tuple(issues), tuple(sorted(incomplete)))
