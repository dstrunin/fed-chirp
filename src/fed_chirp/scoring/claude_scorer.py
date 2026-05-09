"""Score a Federal Reserve speech for hawkish/dovish tone via Claude API.

Uses prompt caching on the system block so the rubric stays warm across the
many speeches scored in a single `scan` or `backfill` run.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass

from anthropic import Anthropic, APIStatusError, RateLimitError

from .prompt import SYSTEM_PROMPT

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_OUTPUT_TOKENS = 800
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0


@dataclass
class ScoreResult:
    score: float | None
    label: str
    rationale: str
    key_quotes: list[str]
    model: str
    scored_at: dt.datetime


def score_speech(
    speaker_name: str,
    speaker_role: str,
    speech_date: dt.date,
    title: str,
    body: str,
    *,
    doc_type: str = "speech",
    model: str = DEFAULT_MODEL,
    client: Anthropic | None = None,
) -> ScoreResult:
    if client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        client = Anthropic(api_key=api_key)

    user_text = (
        f"{_doc_header(doc_type, speaker_name, speaker_role, speech_date)}"
        f"Title: {title}\n\n"
        f"Document body follows. {_scoring_directive(doc_type)}\n\n"
        f"---\n\n"
        f"{body}"
    )

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_text}],
            )
            break
        except (RateLimitError, APIStatusError) as exc:
            last_err = exc
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(RETRY_BASE_DELAY * (2**attempt))
    else:
        raise RuntimeError(f"Exhausted retries: {last_err}")

    raw = "".join(block.text for block in response.content if block.type == "text").strip()
    parsed = _extract_json(raw)
    raw_score = parsed.get("score")
    score: float | None = None if raw_score is None else float(raw_score)
    return ScoreResult(
        score=score,
        label=str(parsed["label"]),
        rationale=str(parsed["rationale"]),
        key_quotes=list(parsed.get("key_quotes", [])),
        model=model,
        scored_at=dt.datetime.now(dt.timezone.utc),
    )


def _doc_header(doc_type: str, speaker_name: str, speaker_role: str, d: dt.date) -> str:
    """Return the document-context preamble for the user message."""
    iso = d.isoformat()
    if doc_type == "fomc_statement":
        return (
            f"Document type: FOMC Statement (committee policy decision; the "
            f"canonical ~400-word policy text released after the meeting).\n"
            f"Date: {iso}\n"
        )
    if doc_type == "fomc_minutes":
        return (
            f"Document type: FOMC Minutes (committee deliberation record from "
            f"the meeting).\n"
            f"Released: {iso}\n"
        )
    if doc_type == "fomc_presser":
        return (
            f"Document type: Press Conference Transcript "
            f"(Chair Powell prepared remarks + reporter Q&A).\n"
            f"Date: {iso}\n"
        )
    # speech (default)
    return (
        f"Speaker: {speaker_name} ({speaker_role})\n"
        f"Date: {iso}\n"
    )


def _scoring_directive(doc_type: str) -> str:
    if doc_type == "fomc_statement":
        return (
            "Score the committee's overall policy stance reflected in this "
            "statement. Even small word changes vs the prior statement matter."
        )
    if doc_type == "fomc_minutes":
        return (
            "Score the OVERALL committee stance reflected in the minutes; "
            "weight the consensus position more than dissenting member views."
        )
    if doc_type == "fomc_presser":
        return (
            "Score Powell's policy stance across both prepared remarks and Q&A. "
            "Q&A answers often shift more weight than the prepared statement."
        )
    return "Score the speaker's policy stance."


def _extract_json(s: str) -> dict:
    """Strip an optional ```json fence and parse the JSON object inside."""
    s = s.strip()
    if s.startswith("```"):
        # Drop the opening fence (with optional language tag) and the closing fence.
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1 :]
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned non-JSON: {s[:200]!r}") from exc
