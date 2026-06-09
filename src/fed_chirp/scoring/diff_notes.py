"""Auto-generate explanatory notes for FOMC-statement diffs via Hermes.

Statements are ~400 words. The Fed-watcher exercise is reading the diff
vs the prior statement and translating wording shifts into policy signal
("dropped 'somewhat' qualifying inflation → less hedged on persistence").
Stored as JSON list-of-strings on `speech_scores.diff_notes`.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

from .hermes_client import HermesClient

DEFAULT_MODEL = "gpt-5.5"
MAX_OUTPUT_TOKENS = 700
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0

SYSTEM_PROMPT = """\
You are an expert reader of FOMC monetary-policy statements. You will be \
given two consecutive statements and the hawk/dove score each one received \
on a -2 (very dovish) to +2 (very hawkish) scale.

Your job is to produce a short list of bullet notes (3-5) that explain the \
meaningful changes between the two statements and what each change signals \
about the committee's policy stance.

# What counts as a meaningful change

- **Qualifier shifts** on inflation/employment language (e.g. "somewhat
  elevated" → "elevated" → "moderating"; "solid" → "moderate" → "softening")
- **Reaction-function language** (e.g. "carefully assess" vs "anticipates
  that further policy firming may be appropriate" vs "in considering the
  extent and timing of any reduction")
- **Forward-guidance** changes (insertion/removal of phrases like "for some
  time", "until inflation has returned sustainably to 2 percent")
- **Risk-balance** language ("risks to both sides", "downside risks have
  diminished", "balanced", "weighted to the downside")
- **Dissents** — who voted no, what they preferred, and any new dissenter
  pattern (e.g. multiple dissents over inclusion/exclusion of specific
  language)
- **Rate decision changes** (hike/hold/cut, balance-sheet runoff pace)

# What to ignore

- Boilerplate at the bottom about the implementation note
- Date stamps and procedural language
- Roster updates that just reflect the natural rotation of voting members
  (unless they accompany a substantive dissent)

# Output format

Return ONE JSON object with a single key, `notes`, whose value is a list of
3 to 5 short bullet strings. Each bullet should:

1. Cite the specific wording change. **Quote wording snippets with SINGLE
   quotes (apostrophes), never double quotes** — double quotes inside the
   JSON string will break parsing.
2. End with a phrase explaining what it signals (more hawkish / more
   dovish / no real change in stance / etc.)

Keep each bullet under ~30 words. Use markdown `**bold**` and `*italic*`
freely, but DO NOT use double quotes anywhere inside a bullet.

Example output shape:

{
  "notes": [
    "**'somewhat elevated' → 'elevated'** on inflation: drop of the qualifier signals less hedging on persistence — *modestly hawkish*.",
    "**Added 'easing bias'** language to reaction function: explicit tilt toward cuts — *dovish*.",
    "**Three new dissents** (Hammack, Kashkari, Logan) opposing easing-bias inclusion: signals real internal disagreement on the pivot — *committee less unified*."
  ]
}

Return JSON only, no prose, no markdown fences.
"""


def annotate_statement_diff(
    *,
    prior_body: str,
    prior_date: dt.date,
    prior_score: float,
    current_body: str,
    current_date: dt.date,
    current_score: float,
    model: str = DEFAULT_MODEL,
    client: HermesClient | None = None,
) -> list[str]:
    if client is None:
        client = HermesClient.from_env()
    if model != DEFAULT_MODEL and client.model != model:
        client = replace(client, model=model)

    user_text = (
        f"Prior statement — {prior_date.isoformat()} — scored {prior_score:+.2f}\n"
        f"---\n{prior_body}\n\n"
        f"=================================================================\n\n"
        f"Current statement — {current_date.isoformat()} — scored {current_score:+.2f}\n"
        f"---\n{current_body}\n"
    )
    prompt = _build_diff_prompt(user_text)

    parsed = client.complete_json(prompt)
    notes = parsed.get("notes")
    if not isinstance(notes, list):
        raise ValueError("Expected JSON object with 'notes' list")
    return [str(n) for n in notes]


def _build_diff_prompt(user_text: str) -> str:
    return (
        "You are running inside Fed Chirp, a local Federal Reserve communications "
        "monitor. Follow the instructions exactly and return JSON only.\n\n"
        "# Diff-note instructions\n"
        f"{SYSTEM_PROMPT}\n\n"
        "# Statements to compare\n"
        f"{user_text}\n\n"
        "Return ONE JSON object and nothing else. Do not use markdown fences."
    )


def _extract_json(s: str) -> dict:
    """Legacy parser kept for older tests/imports; HermesClient handles new calls."""
    s = s.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        import re as _re
        bullets = _re.findall(r'"((?:[^"\\]|\\.)*?)"\s*[,\]]', s, flags=_re.DOTALL)
        bullets = [b for b in bullets if b.strip().lower() != "notes"]
        if bullets:
            return {"notes": bullets}
        raise ValueError(f"Model returned non-JSON: {s[:200]!r}")
