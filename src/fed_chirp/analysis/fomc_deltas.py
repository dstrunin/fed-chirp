"""Alert logic for FOMC documents.

FOMC statements/minutes/pressers each happen ~8x/year. The trailing-90-day
baseline used for individual governor speeches is too sparse here (you'd
have at most 1-2 prior docs). Instead we compare each new doc's score to
the *most recent prior doc of the same kind* and alert on a single-step
delta.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# Single-step alert threshold. Statements move slowly so even +/-0.5 is
# meaningful; pressers and minutes are noisier but still worth flagging at
# this level.
DELTA_THRESHOLD = 0.5


@dataclass(frozen=True)
class FomcAlertDecision:
    fire: bool
    delta: float                # score - prior_score, or 0 when no prior
    prior_url: str | None
    prior_date: dt.date | None
    prior_score: float | None


def should_alert_doc(
    score: float,
    prior: tuple[str, dt.date, float] | None,
) -> FomcAlertDecision:
    """`prior` is (url, date, score) of the most recent same-doc-type
    document strictly before this one, or None if this is the first one
    we've ever scored."""
    if prior is None:
        return FomcAlertDecision(
            fire=False, delta=0.0, prior_url=None, prior_date=None, prior_score=None
        )
    prior_url, prior_date, prior_score = prior
    delta = score - prior_score
    return FomcAlertDecision(
        fire=abs(delta) >= DELTA_THRESHOLD,
        delta=delta,
        prior_url=prior_url,
        prior_date=prior_date,
        prior_score=prior_score,
    )
