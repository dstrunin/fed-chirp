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
from dataclasses import dataclass

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
