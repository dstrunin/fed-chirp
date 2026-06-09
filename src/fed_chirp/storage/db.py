"""SQLite storage for speeches, scores, and alert history."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_WHITESPACE_RE = re.compile(r"\s+")


def content_hash(body: str) -> str:
    """SHA-256 of a normalized speech body. Lowercased, whitespace-collapsed.

    Used as the dedup key alongside (speaker_key, speech_date) so that the
    same speech served under two URL slugs is only stored once.
    """
    norm = _WHITESPACE_RE.sub(" ", body.strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

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

CREATE TABLE IF NOT EXISTS futures_settlements (
    contract_symbol TEXT NOT NULL,        -- e.g. ZQM26.CBT
    contract_month  TEXT NOT NULL,        -- ISO month, e.g. 2026-06
    settle_date     TEXT NOT NULL,        -- ISO date
    settle_price    REAL NOT NULL,
    implied_rate    REAL NOT NULL,        -- 100 - settle_price
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (contract_symbol, settle_date)
);

CREATE INDEX IF NOT EXISTS idx_futures_settle_date ON futures_settlements(settle_date);

CREATE TABLE IF NOT EXISTS fomc_meetings (
    meeting_date    TEXT PRIMARY KEY,
    has_press_conf  INTEGER NOT NULL DEFAULT 1,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_reactions (
    meeting_date          TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    statement_release_dt  TEXT NOT NULL,        -- 14:00 ET, statement release
    presser_start_dt      TEXT NOT NULL,        -- 14:30 ET, presser start
    eod_close_dt          TEXT NOT NULL,        -- 16:00 ET same day, cash close
    nextday_close_dt      TEXT NOT NULL,        -- 16:00 ET next trading day
    bar_interval          TEXT NOT NULL,        -- '1m'|'5m'|'15m'|'1h'|'none'
    -- Statement window: 14:00 ET -> 14:30 ET (NULL on coarse bars)
    stmt_open             REAL,
    stmt_close            REAL,
    stmt_high             REAL,
    stmt_low              REAL,
    stmt_pct_change       REAL,
    stmt_realized_vol     REAL,
    stmt_range_pct        REAL,
    stmt_max_move_pct     REAL,
    -- Same-day (EOD) window: 14:30 ET -> 16:00 ET (NULL on coarse bars)
    eod_open              REAL,
    eod_close             REAL,
    eod_high              REAL,
    eod_low               REAL,
    eod_pct_change        REAL,
    eod_realized_vol      REAL,
    eod_range_pct         REAL,
    eod_max_move_pct      REAL,
    -- Next-day-close window: 14:30 ET -> next trading day 16:00 ET
    nextday_open          REAL,
    nextday_close         REAL,
    nextday_high          REAL,
    nextday_low           REAL,
    nextday_pct_change    REAL,
    nextday_realized_vol  REAL,
    nextday_range_pct     REAL,
    nextday_max_move_pct  REAL,
    fetched_at            TEXT NOT NULL,
    PRIMARY KEY (meeting_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_market_reactions_date ON market_reactions(meeting_date);

-- Records every speech that the pipeline saw but did NOT produce a score for,
-- so the dashboard can surface them for transparency. Reasons:
--   "fetch_failed"        -> exception during fetch (network, parse, etc.)
--   "captions_unavailable"-> YouTube subtitles disabled/missing
--   "filter_rejected"     -> speech-likeness filter said no (empty/short/nav)
--   "rubric_excluded"     -> scoring rubric returned score=null (non-MP topic or junk)
CREATE TABLE IF NOT EXISTS processing_skips (
    url            TEXT PRIMARY KEY,
    speaker_key    TEXT NOT NULL,
    pub_date       TEXT,
    reason         TEXT NOT NULL,
    message        TEXT,
    recorded_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skips_pub_date ON processing_skips(pub_date);
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
class MarketReaction:
    """Realized intraday move on an index future around an FOMC meeting.

    Three windows, all measured on the same ticker:
      - stmt_*:    statement release (14:00 ET) -> presser start (14:30 ET)
      - eod_*:     presser start (14:30 ET)     -> cash close (16:00 ET) same day
      - nextday_*: presser start (14:30 ET)     -> cash close (16:00 ET) next trading day

    Short windows (stmt, eod) become NULL when the bar interval is too coarse
    to fit 2+ bars (1h bars on meetings older than ~60 days). The 25.5h
    next-day window is always populated when any bars exist.
    """
    meeting_date: dt.date
    ticker: str
    statement_release_dt: dt.datetime
    presser_start_dt: dt.datetime
    eod_close_dt: dt.datetime
    nextday_close_dt: dt.datetime
    bar_interval: str
    # Statement window
    stmt_open: float | None
    stmt_close: float | None
    stmt_high: float | None
    stmt_low: float | None
    stmt_pct_change: float | None
    stmt_realized_vol: float | None
    stmt_range_pct: float | None
    stmt_max_move_pct: float | None
    # Same-day (presser -> 16:00 ET) window
    eod_open: float | None
    eod_close: float | None
    eod_high: float | None
    eod_low: float | None
    eod_pct_change: float | None
    eod_realized_vol: float | None
    eod_range_pct: float | None
    eod_max_move_pct: float | None
    # Next-day close window
    nextday_open: float | None
    nextday_close: float | None
    nextday_high: float | None
    nextday_low: float | None
    nextday_pct_change: float | None
    nextday_realized_vol: float | None
    nextday_range_pct: float | None
    nextday_max_move_pct: float | None
    fetched_at: dt.datetime


@dataclass
class ProcessingSkip:
    url: str
    speaker_key: str
    pub_date: dt.date | None
    reason: str
    message: str
    recorded_at: dt.datetime


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
            if "content_hash" not in cols:
                conn.execute("ALTER TABLE speeches ADD COLUMN content_hash TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_speeches_dedup "
                "ON speeches(speaker_key, speech_date, content_hash)"
            )

            # Migration: market_reactions schema changed pres_* -> eod_*/nextday_*.
            # The data is fully re-derivable from yfinance, so we just drop and
            # let the recreate above (run again below) install the new shape.
            mr_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(market_reactions)")
            }
            if mr_cols and "pres_open" in mr_cols and "eod_open" not in mr_cols:
                conn.execute("DROP TABLE market_reactions")
                conn.executescript(SCHEMA)

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

    def insert_speech(self, speech: StoredSpeech) -> str:
        """Insert speech; return the canonical URL stored.

        If a row with the same (speaker_key, speech_date, content_hash) already
        exists under a different URL, no new row is written and that existing
        URL is returned. The caller should use the returned URL for downstream
        score insertion and baseline lookups.
        """
        h = content_hash(speech.body)
        with self.connect() as conn:
            existing = conn.execute(
                """SELECT url FROM speeches
                   WHERE speaker_key = ? AND speech_date = ?
                     AND content_hash = ? AND url != ?
                   LIMIT 1""",
                (
                    speech.speaker_key,
                    speech.speech_date.isoformat(),
                    h,
                    speech.url,
                ),
            ).fetchone()
            if existing is not None:
                return existing["url"]
            conn.execute(
                """INSERT OR REPLACE INTO speeches
                   (url, speaker_key, speech_date, title, location, body,
                    fetched_at, doc_type, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    speech.url,
                    speech.speaker_key,
                    speech.speech_date.isoformat(),
                    speech.title,
                    speech.location,
                    speech.body,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    speech.doc_type,
                    h,
                ),
            )
        return speech.url

    # ---- scores ----

    def has_score(self, url: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM speech_scores WHERE speech_url = ?", (url,)
            ).fetchone()
            return row is not None

    def delete_score(self, url: str) -> bool:
        """Drop any score row for `url`. Returns True if a row was deleted."""
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM speech_scores WHERE speech_url = ?", (url,),
            )
            return cur.rowcount > 0

    def insert_score(
        self,
        speech_url: str,
        score: float | None,
        label: str,
        rationale: str,
        key_quotes: list[str],
        model: str,
        scored_at: dt.datetime,
    ) -> None:
        if score is None:
            return  # excluded; caller is responsible for logging
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

    def scores_since(self, asof: dt.datetime) -> list[StoredScore]:
        """Return all StoredScores with `scored_at` >= `asof`, oldest first.

        Used by `fed-chirp scan` to build a "what landed this run" summary
        once the scan completes. `asof` should be a timezone-aware UTC
        datetime; we compare against the stored ISO string lexically (the
        scored_at values are written in UTC ISO 8601 elsewhere in this
        module, so lexical comparison is equivalent to time comparison).
        """
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT s.url, s.speaker_key, s.speech_date, s.doc_type, s.title,
                          sc.score, sc.label, sc.rationale, sc.key_quotes,
                          sc.model, sc.scored_at, sc.diff_notes
                   FROM speeches s
                   JOIN speech_scores sc ON sc.speech_url = s.url
                   WHERE sc.scored_at >= ?
                   ORDER BY sc.scored_at""",
                (asof.isoformat(),),
            ).fetchall()
        return [_row_to_score(r) for r in rows]

    # ---- processing skips (transparency log) ----

    def record_skip(
        self,
        url: str,
        speaker_key: str,
        pub_date: dt.date | None,
        reason: str,
        message: str,
    ) -> None:
        """Log a speech the pipeline declined to score.

        Idempotent on `url`: re-running the pipeline on the same URL just
        updates the reason/message/timestamp.
        """
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO processing_skips
                   (url, speaker_key, pub_date, reason, message, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    url,
                    speaker_key,
                    pub_date.isoformat() if pub_date else None,
                    reason,
                    message,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                ),
            )

    def clear_skip(self, url: str) -> None:
        """Remove a URL's skip record. Called when the URL eventually succeeds."""
        with self.connect() as conn:
            conn.execute("DELETE FROM processing_skips WHERE url = ?", (url,))

    def recent_skips(self, since: dt.date) -> list[ProcessingSkip]:
        """Skips with pub_date on/after `since`, newest first."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT url, speaker_key, pub_date, reason, message, recorded_at
                   FROM processing_skips
                   WHERE pub_date IS NOT NULL AND pub_date >= ?
                   ORDER BY pub_date DESC""",
                (since.isoformat(),),
            ).fetchall()
        out: list[ProcessingSkip] = []
        for r in rows:
            out.append(
                ProcessingSkip(
                    url=r["url"],
                    speaker_key=r["speaker_key"],
                    pub_date=dt.date.fromisoformat(r["pub_date"]) if r["pub_date"] else None,
                    reason=r["reason"],
                    message=r["message"] or "",
                    recorded_at=dt.datetime.fromisoformat(r["recorded_at"]),
                )
            )
        return out

    def last_speech_dates(self, doc_type: str = "speech") -> dict[str, dt.date]:
        """Return {speaker_key: latest_speech_date} across the speeches table.

        Used by the coverage-health check. Defaults to doc_type='speech' so
        FOMC docs (which are stored under a synthetic FOMC speaker key) don't
        skew the per-speaker freshness signal.
        """
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT speaker_key, MAX(speech_date) AS d
                   FROM speeches
                   WHERE doc_type = ?
                   GROUP BY speaker_key""",
                (doc_type,),
            ).fetchall()
        return {r["speaker_key"]: dt.date.fromisoformat(r["d"]) for r in rows}

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

    # ---- futures settlements ----

    def insert_settlement(
        self,
        contract_symbol: str,
        contract_month: str,
        settle_date: dt.date,
        settle_price: float,
    ) -> None:
        implied_rate = 100.0 - settle_price
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO futures_settlements
                   (contract_symbol, contract_month, settle_date,
                    settle_price, implied_rate, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    contract_symbol,
                    contract_month,
                    settle_date.isoformat(),
                    settle_price,
                    implied_rate,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                ),
            )

    def latest_chain(self) -> list[tuple[str, str, dt.date, float, float]]:
        """Return the chain on the most recent settle_date.
        Each tuple: (contract_symbol, contract_month, settle_date, price, implied_rate).
        Sorted by contract_month ascending."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(settle_date) AS d FROM futures_settlements"
            ).fetchone()
            if row is None or row["d"] is None:
                return []
            asof = row["d"]
            rows = conn.execute(
                """SELECT contract_symbol, contract_month, settle_date,
                          settle_price, implied_rate
                   FROM futures_settlements
                   WHERE settle_date = ?
                   ORDER BY contract_month""",
                (asof,),
            ).fetchall()
        return [
            (
                r["contract_symbol"],
                r["contract_month"],
                dt.date.fromisoformat(r["settle_date"]),
                r["settle_price"],
                r["implied_rate"],
            )
            for r in rows
        ]

    # ---- FOMC meetings ----

    def upsert_meeting(self, meeting_date: dt.date, has_press_conf: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO fomc_meetings
                   (meeting_date, has_press_conf, fetched_at)
                   VALUES (?, ?, ?)""",
                (
                    meeting_date.isoformat(),
                    1 if has_press_conf else 0,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                ),
            )

    def upcoming_meetings(self, asof: dt.date, limit: int = 4) -> list[dt.date]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT meeting_date FROM fomc_meetings
                   WHERE meeting_date >= ?
                   ORDER BY meeting_date
                   LIMIT ?""",
                (asof.isoformat(), limit),
            ).fetchall()
        return [dt.date.fromisoformat(r["meeting_date"]) for r in rows]

    def all_meetings(self) -> list[dt.date]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT meeting_date FROM fomc_meetings ORDER BY meeting_date"
            ).fetchall()
        return [dt.date.fromisoformat(r["meeting_date"]) for r in rows]

    # ---- market reactions ----

    def insert_market_reaction(self, reaction: MarketReaction) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO market_reactions (
                    meeting_date, ticker,
                    statement_release_dt, presser_start_dt,
                    eod_close_dt, nextday_close_dt,
                    bar_interval,
                    stmt_open, stmt_close, stmt_high, stmt_low,
                    stmt_pct_change, stmt_realized_vol,
                    stmt_range_pct, stmt_max_move_pct,
                    eod_open, eod_close, eod_high, eod_low,
                    eod_pct_change, eod_realized_vol,
                    eod_range_pct, eod_max_move_pct,
                    nextday_open, nextday_close, nextday_high, nextday_low,
                    nextday_pct_change, nextday_realized_vol,
                    nextday_range_pct, nextday_max_move_pct,
                    fetched_at
                ) VALUES (
                    ?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,
                    ?
                )""",
                (
                    reaction.meeting_date.isoformat(),
                    reaction.ticker,
                    reaction.statement_release_dt.isoformat(),
                    reaction.presser_start_dt.isoformat(),
                    reaction.eod_close_dt.isoformat(),
                    reaction.nextday_close_dt.isoformat(),
                    reaction.bar_interval,
                    reaction.stmt_open, reaction.stmt_close,
                    reaction.stmt_high, reaction.stmt_low,
                    reaction.stmt_pct_change, reaction.stmt_realized_vol,
                    reaction.stmt_range_pct, reaction.stmt_max_move_pct,
                    reaction.eod_open, reaction.eod_close,
                    reaction.eod_high, reaction.eod_low,
                    reaction.eod_pct_change, reaction.eod_realized_vol,
                    reaction.eod_range_pct, reaction.eod_max_move_pct,
                    reaction.nextday_open, reaction.nextday_close,
                    reaction.nextday_high, reaction.nextday_low,
                    reaction.nextday_pct_change, reaction.nextday_realized_vol,
                    reaction.nextday_range_pct, reaction.nextday_max_move_pct,
                    reaction.fetched_at.isoformat(),
                ),
            )

    def get_market_reactions(self, limit: int | None = None) -> list[MarketReaction]:
        sql = (
            "SELECT * FROM market_reactions "
            "ORDER BY meeting_date DESC, ticker ASC"
        )
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_reaction(r) for r in rows]

    def has_market_reaction(self, meeting_date: dt.date, ticker: str) -> bool:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT 1 FROM market_reactions WHERE meeting_date = ? AND ticker = ?",
                (meeting_date.isoformat(), ticker),
            ).fetchone()
        return r is not None

    def meeting_dates_with_presser(self) -> list[dt.date]:
        """Distinct meeting dates that have an FOMC press-conference doc
        already stored (presser PDF was found and ingested)."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT speech_date FROM speeches
                   WHERE doc_type = 'fomc_presser'
                   ORDER BY speech_date DESC"""
            ).fetchall()
        return [dt.date.fromisoformat(r["speech_date"]) for r in rows]

    def latest_calendar_fetch(self) -> dt.datetime | None:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT MAX(fetched_at) AS f FROM fomc_meetings"
            ).fetchone()
        if r is None or r["f"] is None:
            return None
        return dt.datetime.fromisoformat(r["f"])


def _row_to_reaction(r: sqlite3.Row) -> MarketReaction:
    return MarketReaction(
        meeting_date=dt.date.fromisoformat(r["meeting_date"]),
        ticker=r["ticker"],
        statement_release_dt=dt.datetime.fromisoformat(r["statement_release_dt"]),
        presser_start_dt=dt.datetime.fromisoformat(r["presser_start_dt"]),
        eod_close_dt=dt.datetime.fromisoformat(r["eod_close_dt"]),
        nextday_close_dt=dt.datetime.fromisoformat(r["nextday_close_dt"]),
        bar_interval=r["bar_interval"],
        stmt_open=r["stmt_open"], stmt_close=r["stmt_close"],
        stmt_high=r["stmt_high"], stmt_low=r["stmt_low"],
        stmt_pct_change=r["stmt_pct_change"],
        stmt_realized_vol=r["stmt_realized_vol"],
        stmt_range_pct=r["stmt_range_pct"],
        stmt_max_move_pct=r["stmt_max_move_pct"],
        eod_open=r["eod_open"], eod_close=r["eod_close"],
        eod_high=r["eod_high"], eod_low=r["eod_low"],
        eod_pct_change=r["eod_pct_change"],
        eod_realized_vol=r["eod_realized_vol"],
        eod_range_pct=r["eod_range_pct"],
        eod_max_move_pct=r["eod_max_move_pct"],
        nextday_open=r["nextday_open"], nextday_close=r["nextday_close"],
        nextday_high=r["nextday_high"], nextday_low=r["nextday_low"],
        nextday_pct_change=r["nextday_pct_change"],
        nextday_realized_vol=r["nextday_realized_vol"],
        nextday_range_pct=r["nextday_range_pct"],
        nextday_max_move_pct=r["nextday_max_move_pct"],
        fetched_at=dt.datetime.fromisoformat(r["fetched_at"]),
    )


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
