"""Fed Chirp CLI — discover, score, and report on Fed Board speeches and FOMC docs."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import click
from dotenv import load_dotenv

from .analysis.deltas import baseline as build_baseline
from .analysis.deltas import should_alert
from .analysis.fomc_deltas import should_alert_doc
from .fetchers import fomc as fomc_fetch
from .fetchers.federalreserve import (
    Speaker,
    SpeechRef,
    discover,
    fetch_speech,
    load_speakers,
)
from .output import dashboard as dash
from .output import diff as diff_render
from .output.email_report import AlertItem, FomcAlertItem, send_alerts
from .scoring.claude_scorer import score_speech
from .scoring.diff_notes import annotate_statement_diff
from .storage.db import Database, StoredSpeech
from .utils.log import get_logger

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "fed_chirp.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "speakers.yaml"
DEFAULT_DASHBOARD = PROJECT_ROOT / "dashboard" / "index.html"

log = get_logger()


@click.group()
def cli() -> None:
    """Monitor Federal Reserve Board governor speech tone + FOMC docs."""
    load_dotenv(PROJECT_ROOT / ".env", override=True)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Don't send email; print to stdout.")
@click.option("--config", type=click.Path(path_type=Path), default=DEFAULT_CONFIG)
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
def scan(dry_run: bool, config: Path, db_path: Path) -> None:
    """Discover, fetch, and score new speeches + FOMC docs; alert on shifts."""
    speakers = load_speakers(config)
    db = Database(db_path)

    speech_refs = discover(speakers)
    new_speech = [r for r in speech_refs if not db.has_score(r.url)]
    log.info("speeches: %d in feeds, %d new", len(speech_refs), len(new_speech))

    fomc_refs = fomc_fetch.discover()
    new_fomc = [r for r in fomc_refs if not db.has_score(r.url)]
    log.info("fomc docs: %d in feeds, %d new", len(fomc_refs), len(new_fomc))

    speech_alerts = _process_speeches(new_speech, speakers, db)
    fomc_alerts = _process_fomc(new_fomc, db)
    _regenerate_dashboard(speakers, db)

    all_alerts: list = list(speech_alerts) + list(fomc_alerts)
    if all_alerts:
        send_alerts(all_alerts, dry_run=dry_run)
        if not dry_run:
            for a in all_alerts:
                z = getattr(a.decision, "z_score", None)
                db.record_alert(a.speech_url, a.decision.delta, z)
        log.info("alerts: %d sent (dry_run=%s)", len(all_alerts), dry_run)
    else:
        log.info("no alerts this run")


@cli.command()
@click.option("--since", "since_str", required=True, help="ISO date, e.g. 2026-01-01.")
@click.option("--config", type=click.Path(path_type=Path), default=DEFAULT_CONFIG)
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
def backfill(since_str: str, config: Path, db_path: Path) -> None:
    """Fetch and score every speech + FOMC doc since a given date.

    Skips URLs already scored. Does NOT send emails.
    """
    since_date = dt.date.fromisoformat(since_str)
    speakers = load_speakers(config)
    db = Database(db_path)

    speech_refs = discover(speakers, since=since_date)
    new_speech = [r for r in speech_refs if not db.has_score(r.url)]
    log.info("backfill speeches since %s: %d in feeds, %d to process",
             since_date, len(speech_refs), len(new_speech))

    fomc_refs = fomc_fetch.discover(since=since_date)
    new_fomc = [r for r in fomc_refs if not db.has_score(r.url)]
    log.info("backfill fomc docs since %s: %d in feeds, %d to process",
             since_date, len(fomc_refs), len(new_fomc))

    _process_speeches(new_speech, speakers, db, suppress_alerts=True)
    _process_fomc(new_fomc, db, suppress_alerts=True)
    _regenerate_dashboard(speakers, db)


@cli.command()
@click.option("--config", type=click.Path(path_type=Path), default=DEFAULT_CONFIG)
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
def dashboard(config: Path, db_path: Path) -> None:
    """Regenerate the local HTML dashboard from existing data."""
    speakers = load_speakers(config)
    db = Database(db_path)
    _regenerate_dashboard(speakers, db)
    click.echo(f"wrote {DEFAULT_DASHBOARD}")


@cli.command("score-one")
@click.argument("url")
@click.option("--config", type=click.Path(path_type=Path), default=DEFAULT_CONFIG)
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
def score_one(url: str, config: Path, db_path: Path) -> None:
    """Fetch and score a single URL (debug). Auto-detects doc type from URL."""
    speakers = load_speakers(config)
    db = Database(db_path)

    if "/pressreleases/monetary" in url or "FOMCpresconf" in url:
        # FOMC document path
        ref = _fomc_ref_from_url(url)
        doc = fomc_fetch.fetch_doc(ref)
        db.insert_speech(StoredSpeech(
            url=doc.url,
            speaker_key=doc.speaker_key,
            speech_date=doc.speech_date,
            title=doc.title,
            location="",
            body=doc.body,
            doc_type=doc.doc_type,
        ))
        speaker_name = (
            "FOMC Committee" if doc.speaker_key == fomc_fetch.FOMC_SPEAKER_KEY
            else "Jerome H. Powell"
        )
        speaker_role = (
            "Committee" if doc.speaker_key == fomc_fetch.FOMC_SPEAKER_KEY else "Chair"
        )
        result = score_speech(
            speaker_name=speaker_name,
            speaker_role=speaker_role,
            speech_date=doc.speech_date,
            title=doc.title,
            body=doc.body,
            doc_type=doc.doc_type,
        )
        db.insert_score(
            speech_url=doc.url,
            score=result.score,
            label=result.label,
            rationale=result.rationale,
            key_quotes=result.key_quotes,
            model=result.model,
            scored_at=result.scored_at,
        )
        click.echo(
            f"{fomc_fetch.doc_type_label(doc.doc_type)} — {doc.speech_date.isoformat()} — "
            f"score {result.score:+.2f} ({result.label})"
        )
        click.echo(result.rationale)
        for q in result.key_quotes:
            click.echo(f"  - {q!r}")
        return

    # Speech path (existing behavior)
    speaker = _resolve_speaker_from_url(url, speakers)
    if speaker is None:
        raise click.ClickException(f"Could not resolve speaker from URL: {url}")

    ref = SpeechRef(url=url, speaker_key=speaker.key, title="", pub_date=dt.date.today())
    speech = fetch_speech(ref)
    db.insert_speech(StoredSpeech(
        url=speech.url,
        speaker_key=speech.speaker_key,
        speech_date=speech.speech_date,
        title=speech.title,
        location=speech.location,
        body=speech.body,
    ))
    result = score_speech(
        speaker_name=speaker.name,
        speaker_role=speaker.role,
        speech_date=speech.speech_date,
        title=speech.title,
        body=speech.body,
    )
    db.insert_score(
        speech_url=speech.url,
        score=result.score,
        label=result.label,
        rationale=result.rationale,
        key_quotes=result.key_quotes,
        model=result.model,
        scored_at=result.scored_at,
    )
    click.echo(
        f"{speaker.name} — {speech.speech_date.isoformat()} — "
        f"score {result.score:+.2f} ({result.label})"
    )
    click.echo(result.rationale)
    for q in result.key_quotes:
        click.echo(f"  - {q!r}")


@cli.command("diff")
@click.argument("url")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
def diff_cmd(url: str, db_path: Path) -> None:
    """Print a word-diff of an FOMC statement vs the previous statement,
    followed by the auto-generated explanation notes (if available).

    Both documents must already be in the local DB (run `backfill` or `scan`
    first). Output is ANSI-colored: red = removed, green = added.
    """
    db = Database(db_path)
    cur = db.get_speech(url)
    if cur is None:
        raise click.ClickException(f"Speech not in DB: {url}")
    if cur.doc_type != fomc_fetch.DOC_STATEMENT:
        raise click.ClickException(
            f"diff only supports FOMC statements; this URL is {cur.doc_type}"
        )
    prior = db.prior_doc_score(fomc_fetch.DOC_STATEMENT, cur.speech_date)
    if prior is None:
        raise click.ClickException("No prior statement in DB to diff against.")
    prior_speech = db.get_speech(prior[0])
    assert prior_speech is not None

    click.echo(f"--- prior:   {prior_speech.speech_date.isoformat()}  {prior_speech.url}")
    click.echo(f"+++ current: {cur.speech_date.isoformat()}  {cur.url}")
    click.echo()
    click.echo(diff_render.render_ansi(prior_speech.body, cur.body))

    cur_score = db.get_score(cur.url)
    if cur_score and cur_score.diff_notes:
        click.echo()
        click.echo("=" * 70)
        click.echo("Notes (auto-generated):")
        for n in cur_score.diff_notes:
            click.echo(f"  • {n}")


@cli.command("annotate-diffs")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
def annotate_diffs(db_path: Path) -> None:
    """Backfill diff notes for FOMC statements that have a prior in the DB
    but no notes yet. One Claude call per statement."""
    db = Database(db_path)
    pending = db.statements_missing_notes()
    log.info("annotate-diffs: %d statements missing notes", len(pending))
    for url, prior_url in pending:
        cur_speech = db.get_speech(url)
        prior_speech = db.get_speech(prior_url)
        cur_score = db.get_score(url)
        prior_score = db.get_score(prior_url)
        if not (cur_speech and prior_speech and cur_score and prior_score):
            log.warning("missing data for %s; skipping", url)
            continue
        try:
            notes = annotate_statement_diff(
                prior_body=prior_speech.body,
                prior_date=prior_speech.speech_date,
                prior_score=prior_score.score,
                current_body=cur_speech.body,
                current_date=cur_speech.speech_date,
                current_score=cur_score.score,
            )
            db.set_diff_notes(url, notes)
            log.info("annotated %s -> %d notes", url, len(notes))
        except Exception as exc:
            log.exception("annotate failed: %s — %s", url, exc)
    speakers = load_speakers(DEFAULT_CONFIG)
    _regenerate_dashboard(speakers, db)


# ---- helpers ----


def _process_speeches(
    refs: list[SpeechRef],
    speakers: list[Speaker],
    db: Database,
    *,
    suppress_alerts: bool = False,
) -> list[AlertItem]:
    by_key = {sp.key: sp for sp in speakers}
    alerts: list[AlertItem] = []

    for ref in refs:
        speaker = by_key.get(ref.speaker_key)
        if speaker is None:
            log.warning("unknown speaker_key %s on %s; skipping", ref.speaker_key, ref.url)
            continue
        try:
            speech = fetch_speech(ref)
        except Exception as exc:
            log.exception("fetch failed: %s — %s", ref.url, exc)
            continue

        db.insert_speech(StoredSpeech(
            url=speech.url,
            speaker_key=speech.speaker_key,
            speech_date=speech.speech_date,
            title=speech.title,
            location=speech.location,
            body=speech.body,
        ))

        try:
            result = score_speech(
                speaker_name=speaker.name,
                speaker_role=speaker.role,
                speech_date=speech.speech_date,
                title=speech.title,
                body=speech.body,
            )
        except Exception as exc:
            log.exception("score failed: %s — %s", speech.url, exc)
            continue

        db.insert_score(
            speech_url=speech.url,
            score=result.score,
            label=result.label,
            rationale=result.rationale,
            key_quotes=result.key_quotes,
            model=result.model,
            scored_at=result.scored_at,
        )
        log.info(
            "scored %s %s -> %+.2f (%s)",
            speaker.key, speech.speech_date, result.score, result.label,
        )

        if suppress_alerts:
            continue

        prior = db.speaker_scores_before(speaker.key, speech.speech_date)
        base = build_baseline(prior, speech.speech_date)
        decision = should_alert(result.score, base)
        if decision.fire:
            alerts.append(AlertItem(
                speaker=speaker,
                speech_url=speech.url,
                speech_date=speech.speech_date,
                title=speech.title,
                location=speech.location,
                score=result.score,
                label=result.label,
                rationale=result.rationale,
                key_quotes=result.key_quotes,
                baseline=base,
                decision=decision,
            ))

    return alerts


def _process_fomc(
    refs: list[fomc_fetch.FomcRef],
    db: Database,
    *,
    suppress_alerts: bool = False,
) -> list[FomcAlertItem]:
    alerts: list[FomcAlertItem] = []

    # Sort oldest first so prior_doc_score sees correct chronology when
    # backfilling. Older docs go in before newer ones reference them.
    refs = sorted(refs, key=lambda r: r.pub_date)

    for ref in refs:
        try:
            doc = fomc_fetch.fetch_doc(ref)
        except Exception as exc:
            log.exception("fomc fetch failed: %s — %s", ref.url, exc)
            continue

        db.insert_speech(StoredSpeech(
            url=doc.url,
            speaker_key=doc.speaker_key,
            speech_date=doc.speech_date,
            title=doc.title,
            location="",
            body=doc.body,
            doc_type=doc.doc_type,
        ))

        speaker_name = (
            "FOMC Committee" if doc.speaker_key == fomc_fetch.FOMC_SPEAKER_KEY
            else "Jerome H. Powell"
        )
        speaker_role = (
            "Committee" if doc.speaker_key == fomc_fetch.FOMC_SPEAKER_KEY else "Chair"
        )
        try:
            result = score_speech(
                speaker_name=speaker_name,
                speaker_role=speaker_role,
                speech_date=doc.speech_date,
                title=doc.title,
                body=doc.body,
                doc_type=doc.doc_type,
            )
        except Exception as exc:
            log.exception("fomc score failed: %s — %s", doc.url, exc)
            continue

        db.insert_score(
            speech_url=doc.url,
            score=result.score,
            label=result.label,
            rationale=result.rationale,
            key_quotes=result.key_quotes,
            model=result.model,
            scored_at=result.scored_at,
        )
        log.info(
            "scored %s %s -> %+.2f (%s)",
            doc.doc_type, doc.speech_date, result.score, result.label,
        )

        # For statements with a prior, generate explanatory diff notes.
        # Done regardless of suppress_alerts because the notes are useful
        # in the dashboard whether or not we email this run.
        notes: list[str] | None = None
        prior = db.prior_doc_score(doc.doc_type, doc.speech_date)
        if doc.doc_type == fomc_fetch.DOC_STATEMENT and prior is not None:
            prior_speech = db.get_speech(prior[0])
            if prior_speech is not None:
                try:
                    notes = annotate_statement_diff(
                        prior_body=prior_speech.body,
                        prior_date=prior_speech.speech_date,
                        prior_score=prior[2],
                        current_body=doc.body,
                        current_date=doc.speech_date,
                        current_score=result.score,
                    )
                    db.set_diff_notes(doc.url, notes)
                    log.info("annotated diff: %s -> %d notes", doc.url, len(notes))
                except Exception as exc:
                    log.exception("annotate failed: %s — %s", doc.url, exc)

        if suppress_alerts:
            continue

        decision = should_alert_doc(result.score, prior)
        if decision.fire:
            prior_body = None
            if prior is not None:
                prior_speech = db.get_speech(prior[0])
                prior_body = prior_speech.body if prior_speech else None
            alerts.append(FomcAlertItem(
                doc_type=doc.doc_type,
                speech_url=doc.url,
                speech_date=doc.speech_date,
                title=doc.title,
                score=result.score,
                label=result.label,
                rationale=result.rationale,
                key_quotes=result.key_quotes,
                decision=decision,
                body=doc.body,
                prior_body=prior_body,
                diff_notes=notes,
            ))

    return alerts


def _regenerate_dashboard(speakers: list[Speaker], db: Database) -> None:
    scores = db.all_scores()
    dash.render(speakers, scores, DEFAULT_DASHBOARD)
    log.info("dashboard: regenerated at %s (%d documents)", DEFAULT_DASHBOARD, len(scores))


def _resolve_speaker_from_url(url: str, speakers: list[Speaker]) -> Speaker | None:
    """URLs follow /newsevents/speech/<lastname><yyyymmdd><letter>.htm.
    Match the path slug against each speaker's aliases (case-insensitive).
    """
    lower = url.lower()
    for sp in speakers:
        if sp.key == fomc_fetch.FOMC_SPEAKER_KEY:
            continue
        for alias in sp.aliases:
            if f"/speech/{alias}" in lower:
                return sp
    return None


def _fomc_ref_from_url(url: str) -> fomc_fetch.FomcRef:
    """Build a FomcRef from a raw URL (for `score-one`). Classifies by URL shape."""
    if "FOMCpresconf" in url:
        # /mediacenter/files/FOMCpresconf<yyyymmdd>.pdf
        import re
        m = re.search(r"FOMCpresconf(\d{8})\.pdf$", url)
        if not m:
            raise click.ClickException(f"Cannot parse press-conf URL: {url}")
        ymd = m.group(1)
        d = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        return fomc_fetch.FomcRef(
            url=url, doc_type=fomc_fetch.DOC_PRESSER,
            speaker_key=fomc_fetch.POWELL_SPEAKER_KEY,
            pub_date=d, title=f"Press Conference Transcript ({d.isoformat()})",
        )

    # /pressreleases/monetary<yyyymmdd>a.htm — could be statement or minutes;
    # we can't tell from the URL alone, so fetch and parse the title to decide.
    import requests
    from .fetchers.federalreserve import (
        REQUEST_TIMEOUT, USER_AGENT, parse_article_html,
    )
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    parsed = parse_article_html(
        resp.text, source_url=url, fallback_date=dt.date.today(), fallback_title="",
    )
    title = parsed["title"]
    doc_type = fomc_fetch._classify(title)
    if doc_type is None:
        raise click.ClickException(
            f"URL doesn't look like an FOMC statement or minutes: {title!r}"
        )
    return fomc_fetch.FomcRef(
        url=url, doc_type=doc_type, speaker_key=fomc_fetch.FOMC_SPEAKER_KEY,
        pub_date=parsed["date"], title=title,
    )


if __name__ == "__main__":
    cli()
