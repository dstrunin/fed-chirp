"""Cross-committee divergence: spread between hawks and doves over time.

With 19 speakers (7 Board governors + 12 regional bank presidents) the
spread between the most hawkish and most dovish trailing-90d speaker
means becomes a meaningful signal. Widening divergence often precedes
dissents at the next FOMC meeting.

Pure functions — caller passes pre-fetched scores in. No DB coupling,
no schema changes.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Iterable

from ..storage.db import StoredScore

WINDOW_DAYS = 90
HAWK_THRESHOLD = 0.3
DOVE_THRESHOLD = -0.3


@dataclass(frozen=True)
class SpeakerSnapshot:
    speaker_key: str
    mean: float
    n: int


@dataclass(frozen=True)
class DivergenceSnapshot:
    asof: dt.date
    speakers: list[SpeakerSnapshot]   # only those with ≥1 obs in window
    spread: float                     # max - min over speakers' means
    stdev: float                      # population stdev of means
    hawk_key: str | None              # speaker driving the top
    dove_key: str | None              # speaker driving the bottom
    n_covered: int                    # speakers with data in window
    n_total: int                      # speakers in roster (denominator)


def speaker_means(
    scores: list[StoredScore],
    speaker_keys: Iterable[str],
    asof: dt.date,
    window_days: int = WINDOW_DAYS,
) -> list[SpeakerSnapshot]:
    """Per-speaker trailing-mean over [asof - window_days, asof).

    Only speeches (doc_type == 'speech') with speaker_key in the roster
    are considered. Speakers with zero in-window observations are dropped.
    """
    cutoff = asof - dt.timedelta(days=window_days)
    roster = set(speaker_keys)

    by_speaker: dict[str, list[float]] = {}
    for s in scores:
        if s.doc_type != "speech":
            continue
        if s.speaker_key not in roster:
            continue
        if not (cutoff <= s.speech_date < asof):
            continue
        by_speaker.setdefault(s.speaker_key, []).append(s.score)

    out: list[SpeakerSnapshot] = []
    for key, vals in by_speaker.items():
        out.append(SpeakerSnapshot(
            speaker_key=key,
            mean=sum(vals) / len(vals),
            n=len(vals),
        ))
    out.sort(key=lambda s: s.mean, reverse=True)
    return out


def divergence_snapshot(
    scores: list[StoredScore],
    speaker_keys: Iterable[str],
    asof: dt.date,
    window_days: int = WINDOW_DAYS,
) -> DivergenceSnapshot:
    """Build a full snapshot for a given as-of date."""
    roster = list(speaker_keys)
    snaps = speaker_means(scores, roster, asof, window_days)

    if not snaps:
        return DivergenceSnapshot(
            asof=asof,
            speakers=[],
            spread=0.0,
            stdev=0.0,
            hawk_key=None,
            dove_key=None,
            n_covered=0,
            n_total=len(roster),
        )

    means = [s.mean for s in snaps]
    hi = max(snaps, key=lambda s: s.mean)
    lo = min(snaps, key=lambda s: s.mean)
    spread = hi.mean - lo.mean

    # Population stdev (we have the full set of speaker means in window —
    # not a sample of a larger distribution).
    if len(means) >= 2:
        mu = sum(means) / len(means)
        var = sum((m - mu) ** 2 for m in means) / len(means)
        stdev = math.sqrt(var)
    else:
        stdev = 0.0

    return DivergenceSnapshot(
        asof=asof,
        speakers=snaps,
        spread=spread,
        stdev=stdev,
        hawk_key=hi.speaker_key,
        dove_key=lo.speaker_key,
        n_covered=len(snaps),
        n_total=len(roster),
    )


def time_series(
    scores: list[StoredScore],
    speaker_keys: Iterable[str],
    end_date: dt.date,
    days_back: int = WINDOW_DAYS,
    window_days: int = WINDOW_DAYS,
) -> list[tuple[dt.date, float]]:
    """Daily spread series for the trailing `days_back` days ending at
    `end_date` (inclusive). Each point is the spread of a trailing
    `window_days` window ending at that date.

    Returns ordered oldest -> newest.
    """
    roster = list(speaker_keys)
    out: list[tuple[dt.date, float]] = []
    for offset in range(days_back, -1, -1):
        d = end_date - dt.timedelta(days=offset)
        snap = divergence_snapshot(scores, roster, d, window_days)
        out.append((d, snap.spread))
    return out


def camps(
    snap: DivergenceSnapshot,
    *,
    hawk_threshold: float = HAWK_THRESHOLD,
    dove_threshold: float = DOVE_THRESHOLD,
) -> tuple[list[SpeakerSnapshot], list[SpeakerSnapshot], list[SpeakerSnapshot]]:
    """Bucket speakers into (hawks, neutrals, doves), each sorted by mean
    descending within its bucket."""
    hawks: list[SpeakerSnapshot] = []
    neutrals: list[SpeakerSnapshot] = []
    doves: list[SpeakerSnapshot] = []
    for s in snap.speakers:
        if s.mean > hawk_threshold:
            hawks.append(s)
        elif s.mean < dove_threshold:
            doves.append(s)
        else:
            neutrals.append(s)
    hawks.sort(key=lambda s: s.mean, reverse=True)
    neutrals.sort(key=lambda s: s.mean, reverse=True)
    doves.sort(key=lambda s: s.mean)  # most-dovish first within doves
    return hawks, neutrals, doves
