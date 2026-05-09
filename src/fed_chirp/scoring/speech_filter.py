"""Speech-likeness gate that runs before LLM scoring.

Rejects scraped bodies that aren't actually speech transcripts (admin pages,
sidebar/footer fragments, navigation lists, empty bodies). Without this gate
the rubric is forced to invent a score from priors when there's no body, which
pollutes per-speaker averages.

Policy: a body must be substantive *prose* — long enough and not nav-shaped.
Topic relevance is the RUBRIC's job, not this filter's. A 1500-word speech on
community development is a real speech and should pass; the rubric will score
it 0.0 / neutral. Filtering on monetary-policy keywords here over-rejects
genuinely off-topic-but-real Fed speeches.

FOMC docs bypass entirely (known-good and statements run only ~400 words).

The `keyword_hits` field is still computed for diagnostics but is not gated on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MIN_WORDS = 500
MAX_LINK_DENSITY = 0.35
MIN_BODY_CHARS = 200

# Counted for diagnostics only — not gated. The rubric handles topic relevance.
POLICY_KEYWORDS: tuple[str, ...] = (
    "monetary policy", "federal funds", "fomc", "interest rate", "interest rates",
    "inflation", "disinflation", "price stability", "prices", "cpi", "pce",
    "employment", "unemployment", "labor market", "wage", "wages", "payrolls",
    "rate cut", "rate hike", "tightening", "easing", "restrictive",
    "accommodative", "balance sheet", "runoff", "quantitative", "dual mandate",
    "neutral rate", "r-star", "real rate", "yield", "term premium",
)

_WORD_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class FilterResult:
    passes: bool
    reason: str | None  # "empty" | "too_short" | "nav_text" | None
    word_count: int
    keyword_hits: int  # diagnostic only; not gated
    link_density: float


def _word_count(body: str) -> int:
    return len(_WORD_RE.findall(body))


def _keyword_hits(body_lower: str) -> int:
    return sum(1 for kw in POLICY_KEYWORDS if kw in body_lower)


def _short_line_ratio(body: str) -> float:
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return 0.0
    short = sum(1 for ln in lines if len(_WORD_RE.findall(ln)) <= 6)
    return short / len(lines)


def evaluate(body: str, *, doc_type: str = "speech") -> FilterResult:
    """Decide whether `body` is a real speech worth scoring.

    Non-"speech" doc_types bypass with passes=True; counters are zeroed.
    """
    if doc_type != "speech":
        return FilterResult(True, None, 0, 0, 0.0)

    stripped = body.strip()
    if len(stripped) < MIN_BODY_CHARS:
        return FilterResult(False, "empty", 0, 0, 0.0)

    wc = _word_count(stripped)
    if wc < MIN_WORDS:
        return FilterResult(False, "too_short", wc, 0, 0.0)

    kh = _keyword_hits(stripped.lower())
    density = _short_line_ratio(stripped)
    if density > MAX_LINK_DENSITY:
        return FilterResult(False, "nav_text", wc, kh, density)

    return FilterResult(True, None, wc, kh, density)
