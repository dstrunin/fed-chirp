"""Per-speech deviation vs trailing-90-day baseline."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

WINDOW_DAYS = 90
ABS_THRESHOLD = 1.0
Z_THRESHOLD = 1.5
MIN_BASELINE_N = 5


@dataclass(frozen=True)
class Baseline:
    mean: float
    stdev: float
    n: int


@dataclass(frozen=True)
class AlertDecision:
    fire: bool
    delta: float            # score - baseline.mean
    z_score: float | None   # None when n < 2 or stdev == 0


def baseline(prior_scores: list[tuple[dt.date, float]], asof: dt.date) -> Baseline:
    """Build a baseline from prior (date, score) pairs that fall in the
    trailing WINDOW_DAYS window before `asof`. Excludes the speech itself."""
    cutoff = asof - dt.timedelta(days=WINDOW_DAYS)
    window = [s for d, s in prior_scores if cutoff <= d < asof]
    n = len(window)
    if n == 0:
        return Baseline(mean=0.0, stdev=0.0, n=0)
    mean = sum(window) / n
    if n < 2:
        return Baseline(mean=mean, stdev=0.0, n=n)
    var = sum((x - mean) ** 2 for x in window) / (n - 1)
    return Baseline(mean=mean, stdev=math.sqrt(var), n=n)


def should_alert(score: float, base: Baseline) -> AlertDecision:
    """Decide whether a speech's score is an alert vs the baseline.

    Rules:
      - If n >= MIN_BASELINE_N and stdev > 0: alert when |delta| >= ABS_THRESHOLD
        OR |z| >= Z_THRESHOLD.
      - If 0 < n < MIN_BASELINE_N: alert when |delta| >= ABS_THRESHOLD only.
      - If n == 0: never alert (no baseline yet).
    """
    delta = score - base.mean
    z: float | None = None
    if base.n >= 2 and base.stdev > 0:
        z = delta / base.stdev

    if base.n == 0:
        return AlertDecision(fire=False, delta=delta, z_score=z)

    if base.n >= MIN_BASELINE_N and z is not None:
        fire = abs(delta) >= ABS_THRESHOLD or abs(z) >= Z_THRESHOLD
    else:
        fire = abs(delta) >= ABS_THRESHOLD

    return AlertDecision(fire=fire, delta=delta, z_score=z)
