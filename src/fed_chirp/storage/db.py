"""SQLite storage for speeches, scores, and alert history."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS speeches (
    url            TEXT PRIMARY KEY,
    speaker_key    TEXT NOT NULL,
    speech_date    TEXT NOT NULL,
    title          TEXT NOT NULL,
    location       TEXT,
    body           TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    doc_type       TEXT NOT NULL DEFAULT 'speech'
);

CREATE TABLE IF NOT EXISTS speech_scores (
    speech_url     TEXT PRIMARY KEY REFERENCES speeches(url),
    score          REAL NOT NULL,
    label          TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    key_quotes     TEXT NOT NULL,
    model          TEXT NOT NULL,
    scored_at      TEXT NOT NULL,
    diff_notes     TEXT  -- JSON list of bullet-string notes; only set for FOMC statements with a prior
);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    speech_url     TEXT NOT NULL REFERENCES speeches(url),
    delta          REAL NOT NULL,
    z_score        REAL,
    sent_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_speeches_speaker_date ON speeches(speaker_key, speech_date);
CREATE INDEX IF NOT EXISTS idx_alerts_speech ON alerts(speech_url);
"""


@dataclass
class StoredSpeech:
    url: str
    speaker_key: str
    speech_date: dt.date
    title: str
    location: str
    body: str
    doc_type: str = "speech"


@dataclass
class StoredScore:
    speech_url: str
    speaker_key: str
    speech_date: dt.date
    score: float
    label: str
    rationale: str
    key_quotes: list[str]
    model: str
    scored_at: dt.datetime
    doc_type: str = "speech"
    title: str = ""
    diff_notes: list[str] | None = None  # only set for FOMC statements w/ prior


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Idempotent migration: add doc_type to legacy DBs that predate it.
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(speeches)")}
            if "doc_type" not in cols:
                conn.execute(
                    "ALTER TABLE speeches ADD COLUMN doc_type TEXT NOT NULL DEFAULT 'speech'"
                )
            # Index after the column exists either way.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_speeches_doctype_date "
                "ON speeches(doc_type, speech_date)"
            )
            score_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(speech_scores)")
            }
            if "diff_notes" not in score_cols:
                conn.execute("ALTER TABLE speech_scores ADD COLUMN diff_notes TEXT")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    # ---- speeches ----

    def has_speech(self, url: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM speeches WHERE url = ?", (url,)).fetchone()
            return row is not None

    def insert_speech(self, speech: StoredSpeech) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO speeches
                   (url, speaker_key, speech_date, title, location, body, fetched_at, doc_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    speech.url,
                    speech.speaker_key,
                    speech.speech_date.isoformat(),
                    speech.title,
                    speech.location,
                    speech.body,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    speech.doc_type,
                ),
            )

    # ---- scores ----

    def has_score(self, url: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM speech_scores WHERE speech_url = ?", (url,)
            ).fetchone()
            return row is not None

    def insert_score(
        self,
        speech_url: str,
        score: float,
        label: str,
        rationale: str,
        key_quotes: list[str],
        model: str,
        scored_at: dt.datetime,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO speech_scores
                   (speech_url, score, label, rationale, key_quotes, model, scored_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    speech_url,
                    score,
                    label,
                    rationale,
                    json.dumps(key_quotes),
                    model,
                    scored_at.isoformat(),
                ),
            )

    def speaker_scores_before(
        self, speaker_key: str, asof: dt.date
    ) -> list[tuple[dt.date, float]]:
        """All (date, score) pairs for a speaker strictly before `asof`.

        Restricted to doc_type='speech' so FOMC docs assigned to a
        governor (e.g., Powell pressers) don't pollute the speech baseline.
        """
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT s.speech_date, sc.score
                   FROM speeches s
                   JOIN speech_scores sc ON sc.speech_url = s.url
                   WHERE s.speaker_key = ? AND s.doc_type = 'speech'
                     AND s.speech_date < ?
                   ORDER BY s.speech_date""",
                (speaker_key, asof.isoformat()),
            ).fetchall()
        return [(dt.date.fromisoformat(r["speech_date"]), r["score"]) for r in rows]

    def prior_doc_score(
        self, doc_type: str, asof: dt.date
    ) -> tuple[str, dt.date, float] | None:
        """Most recent doc of `doc_type` strictly before `asof`.

        Returns (url, date, score) or None.
        """
        with self.connect() as conn:
            r = conn.execute(
                """SELECT s.url, s.speech_date, sc.score
                   FROM speeches s
                   JOIN speech_scores sc ON sc.speech_url = s.url
                   WHERE s.doc_type = ? AND s.speech_date < ?
                   ORDER BY s.speech_date DESC
                   LIMIT 1""",
                (doc_type, asof.isoformat()),
            ).fetchone()
        if r is None:
            return None
        return (r["url"], dt.date.fromisoformat(r["speech_date"]), r["score"])

    def docs_by_type(self, doc_type: str, limit: int | None = None) -> list[StoredScore]:
        """Most recent `doc_type` documents that have been scored, newest first."""
        sql = (
            """SELECT s.url, s.speaker_key, s.speech_date, s.doc_type,
                      sc.score, sc.label, sc.rationale, sc.key_quotes,
                      sc.model, sc.scored_at
               FROM speeches s
               JOIN speech_scores sc ON sc.speech_url = s.url
               WHERE s.doc_type = ?
               ORDER BY s.speech_date DESC"""
        )
        params: tuple = (doc_type,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (doc_type, limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_score(r) for r in rows]

    def all_scores(self) -> list[StoredScore]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT s.url, s.speaker_key, s.speech_date, s.doc_type, s.title,
                          sc.score, sc.label, sc.rationale, sc.key_quotes,
                          sc.model, sc.scored_at, sc.diff_notes
                   FROM speeches s
                   JOIN speech_scores sc ON sc.speech_url = s.url
                   ORDER BY s.speech_date DESC""",
            ).fetchall()
        return [_row_to_score(r) for r in rows]

    def get_speech(self, url: str) -> StoredSpeech | None:
        with self.connect() as conn:
            r = conn.execute("SELECT * FROM speeches WHERE url = ?", (url,)).fetchone()
        if r is None:
            return None
        return StoredSpeech(
            url=r["url"],
            speaker_key=r["speaker_key"],
            speech_date=dt.date.fromisoformat(r["speech_date"]),
            title=r["title"],
            location=r["location"] or "",
            body=r["body"],
            doc_type=(r["doc_type"] if "doc_type" in r.keys() else "speech"),
        )

    def get_score(self, url: str) -> StoredScore | None:
        with self.connect() as conn:
            r = conn.execute(
                """SELECT s.url, s.speaker_key, s.speech_date, s.doc_type, s.title,
                          sc.score, sc.label, sc.rationale, sc.key_quotes,
                          sc.model, sc.scored_at, sc.diff_notes
                   FROM speeches s
                   JOIN speech_scores sc ON sc.speech_url = s.url
                   WHERE s.url = ?""",
                (url,),
            ).fetchone()
        if r is None:
            return None
        return _row_to_score(r)

    # ---- alerts ----

    def record_alert(
        self, speech_url: str, delta: float, z_score: float | None
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO alerts (speech_url, delta, z_score, sent_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    speech_url,
                    delta,
                    z_score,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                ),
            )

    def set_diff_notes(self, speech_url: str, notes: list[str]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE speech_scores SET diff_notes = ? WHERE speech_url = ?",
                (json.dumps(notes), speech_url),
            )

    def statements_missing_notes(self) -> list[tuple[str, str]]:
        """Return (url, prior_url) for statements that have a prior statement
        in the DB but no diff_notes yet."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT s.url, s.speech_date
                   FROM speeches s
                   JOIN speech_scores sc ON sc.speech_url = s.url
                   WHERE s.doc_type = 'fomc_statement'
                     AND (sc.diff_notes IS NULL OR sc.diff_notes = '')
                   ORDER BY s.speech_date"""
            ).fetchall()
        out: list[tuple[str, str]] = []
        for r in rows:
            d = dt.date.fromisoformat(r["speech_date"])
            prior = self.prior_doc_score("fomc_statement", d)
            if prior is not None:
                out.append((r["url"], prior[0]))
        return out

    def has_alert(self, speech_url: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM alerts WHERE speech_url = ?", (speech_url,)
            ).fetchone()
            return row is not None


def _row_to_score(r: sqlite3.Row) -> StoredScore:
    keys = r.keys()
    raw_notes = r["diff_notes"] if "diff_notes" in keys else None
    notes: list[str] | None = json.loads(raw_notes) if raw_notes else None
    return StoredScore(
        speech_url=r["url"],
        speaker_key=r["speaker_key"],
        speech_date=dt.date.fromisoformat(r["speech_date"]),
        score=r["score"],
        label=r["label"],
        rationale=r["rationale"],
        key_quotes=json.loads(r["key_quotes"]),
        model=r["model"],
        scored_at=dt.datetime.fromisoformat(r["scored_at"]),
        doc_type=(r["doc_type"] if "doc_type" in keys else "speech"),
        title=(r["title"] if "title" in keys else ""),
        diff_notes=notes,
    )
