"""Score Federal Reserve communications for hawkish/dovish tone via Hermes."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

from .hermes_client import HermesClient
from .prompt import SYSTEM_PROMPT

DEFAULT_MODEL = "gpt-5.5"
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
    client: HermesClient | None = None,
) -> ScoreResult:
    if client is None:
        client = HermesClient.from_env()
    if model != DEFAULT_MODEL and client.model != model:
        client = replace(client, model=model)

    user_text = (
        f"{_doc_header(doc_type, speaker_name, speaker_role, speech_date)}"
        f"Title: {title}\n\n"
        f"Document body follows. {_scoring_directive(doc_type)}\n\n"
        f"---\n\n"
        f"{body}"
    )
    prompt = _build_score_prompt(user_text)

    parsed = client.complete_json(prompt)
    raw_score = parsed.get("score")
    score: float | None = None if raw_score is None else float(raw_score)
    return ScoreResult(
        score=score,
        label=str(parsed["label"]),
        rationale=str(parsed["rationale"]),
        key_quotes=list(parsed.get("key_quotes", [])),
        model=client.model,
        scored_at=dt.datetime.now(dt.timezone.utc),
    )


def _build_score_prompt(user_text: str) -> str:
    return (
        "You are running inside Fed Chirp, a local Federal Reserve communications "
        "monitor. Follow the rubric exactly and return JSON only.\n\n"
        "# Scoring rubric\n"
        f"{SYSTEM_PROMPT}\n\n"
        "# Document to score\n"
        f"{user_text}\n\n"
        "Return ONE JSON object and nothing else. Do not use markdown fences."
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
