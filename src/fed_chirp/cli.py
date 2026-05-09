"""Fed Chirp CLI — discover, score, and report on Fed Board speeches and FOMC docs."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import click
from dotenv import load_dotenv

from .analysis.deltas import baseline as build_baseline
from .analysis.deltas import should_alert
from .analysis import divergence as divergence_analysis
from .analysis.fomc_deltas import should_alert_doc
from .analysis import futures as futures_analysis
from .analysis import health
from .analysis import market_reaction as market_reaction_analysis
from .fetchers import fomc as fomc_fetch
from .fetchers import futures as futures_fetch
from .fetchers import fomc_calendar
from .fetchers import market_data
from .fetchers.federalreserve import (
    Speaker,
    SpeechRef,
    discover,
    fetch_speech,
    load_speakers,
)
from .fetchers.regional import maybe_playwright
from .fetchers.youtube import CaptionsUnavailable
from .output import dashboard as dash
from .output import diff as diff_render
from .output.dashboard import FuturesContext
from .output.email_report import AlertItem, FomcAlertItem, send_alerts
from .scoring import speech_filter
from .scoring.claude_scorer import score_speech
from .scoring.diff_notes import annotate_statement_diff
from .storage.db import Database, MarketReaction, StoredSpeech, content_hash
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

    with maybe_playwright(speakers) as pw:
        speech_refs = discover(speakers, pw=pw)
        new_speech = [r for r in speech_refs if not db.has_score(r.url)]
        log.info("speeches: %d in feeds, %d new", len(speech_refs), len(new_speech))

        fomc_refs = fomc_fetch.discover()
        new_fomc = [r for r in fomc_refs if not db.has_score(r.url)]
        log.info("fomc docs: %d in feeds, %d new", len(fomc_refs), len(new_fomc))

        _refresh_futures_and_calendar(db)

        speech_alerts = _process_speeches(new_speech, speakers, db, pw=pw)
        fomc_alerts = _process_fomc(new_fomc, db)
        _refresh_market_reactions(db)
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
@click.option("--only", "only_keys", multiple=True,
              help="Restrict to one or more speaker_keys (repeatable). Skips FOMC docs.")
@click.option("--config", type=click.Path(path_type=Path), default=DEFAULT_CONFIG)
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
def backfill(
    since_str: str, only_keys: tuple[str, ...], config: Path, db_path: Path
) -> None:
    """Fetch and score every speech + FOMC doc since a given date.

    Skips URLs already scored. Does NOT send emails. With --only, restricts
    discovery to specific speaker_keys and skips the FOMC pipeline.
    """
    since_date = dt.date.fromisoformat(since_str)
    speakers = load_speakers(config)
    db = Database(db_path)

    if only_keys:
        wanted = set(only_keys)
        unknown = wanted - {sp.key for sp in speakers}
        if unknown:
            raise click.ClickException(f"Unknown speaker_key(s): {sorted(unknown)}")
        speakers = [sp for sp in speakers if sp.key in wanted]
        log.info("backfill restricted to: %s", sorted(wanted))

    with maybe_playwright(speakers) as pw:
        speech_refs = discover(speakers, since=since_date, pw=pw)
        new_speech = [r for r in speech_refs if not db.has_score(r.url)]
        log.info("backfill speeches since %s: %d in feeds, %d to process",
                 since_date, len(speech_refs), len(new_speech))

        if not only_keys:
            fomc_refs = fomc_fetch.discover(since=since_date)
            new_fomc = [r for r in fomc_refs if not db.has_score(r.url)]
            log.info("backfill fomc docs since %s: %d in feeds, %d to process",
                     since_date, len(fomc_refs), len(new_fomc))
            _refresh_futures_and_calendar(db)
        else:
            new_fomc = []

        _process_speeches(new_speech, speakers, db, suppress_alerts=True, pw=pw)
        _process_fomc(new_fomc, db, suppress_alerts=True)

    if not only_keys:
        _refresh_market_reactions(db)

    # Always regenerate dashboard against the FULL speaker list, not the
    # --only filtered subset, so the dashboard never loses rows.
    full_speakers = load_speakers(config)
    _regenerate_dashboard(full_speakers, db)


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
        if result.score is None:
            click.echo(
                f"{fomc_fetch.doc_type_label(doc.doc_type)} — {doc.speech_date.isoformat()} — "
                f"EXCLUDED ({result.label})"
            )
            click.echo(result.rationale)
            return
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

    ref = SpeechRef(
        url=url, speaker_key=speaker.key, title="",
        pub_date=dt.date.today(), source=speaker.source,
    )
    with maybe_playwright([speaker]) as pw:
        speech = fetch_speech(ref, pw=pw)
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
    if result.score is None:
        click.echo(
            f"{speaker.name} — {speech.speech_date.isoformat()} — "
            f"EXCLUDED ({result.label})"
        )
        click.echo(result.rationale)
        return
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


@cli.command("futures")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
@click.option("--refresh/--no-refresh", default=True,
              help="Pull fresh settlements before printing.")
def futures_cmd(db_path: Path, refresh: bool) -> None:
    """Print the latest ZQ chain, current rate, market score, and per-meeting
    implied moves with probabilities. Handy for spot-checking against CME's
    public FedWatch page."""
    db = Database(db_path)
    if refresh:
        _refresh_futures_and_calendar(db)

    chain_rows = db.latest_chain()
    if not chain_rows:
        raise click.ClickException("No futures data — run with --refresh.")
    chain = {row[1]: row[4] for row in chain_rows}
    settle_date = chain_rows[0][2]
    all_meetings = db.all_meetings()
    upcoming = db.upcoming_meetings(asof=dt.date.today(), limit=8)
    cur = futures_analysis.current_rate_from_chain(chain, all_meetings)
    if cur is None:
        raise click.ClickException("Could not derive current rate from chain.")

    click.echo(f"Settlement date: {settle_date.isoformat()}")
    click.echo(f"Current effective rate: {cur:.3f}%")
    click.echo()
    click.echo("Chain (implied avg rate by month):")
    for k in sorted(chain.keys()):
        click.echo(f"  {k}: {chain[k]:.3f}%")

    ms, bp = futures_analysis.market_score(chain, cur)
    click.echo()
    click.echo(f"Market score (12m, normalized): {ms:+.2f}  (bp_change_12m: {bp:+.1f})")

    click.echo()
    click.echo("Upcoming meetings:")
    rates = futures_analysis.implied_rates_at_meetings(chain, upcoming, cur)
    for mr in rates[:6]:
        p = futures_analysis.move_probabilities(mr)
        nonzero = {k: v for k, v in p.buckets.items() if v > 0.005}
        fmt = ", ".join(
            f"{int(k):+d}bp:{v*100:.0f}%" for k, v in sorted(nonzero.items())
        )
        click.echo(
            f"  {mr.meeting_date}  {mr.rate_after:.3f}%  "
            f"(Δ {mr.delta_bp:+.1f} bp)  {fmt}"
        )


@cli.command("market-reactions")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
@click.option("--refresh/--no-refresh", default=True,
              help="Pull fresh intraday bars before printing.")
@click.option("--force", is_flag=True,
              help="Re-fetch even meetings already populated.")
def market_reactions_cmd(db_path: Path, refresh: bool, force: bool) -> None:
    """Print the FOMC market-reaction table (ES=F, NQ=F, ZT=F) for each meeting
    with a press conference. Captures the statement window (2:00–2:30pm ET)
    and the post-presser 24h window."""
    db = Database(db_path)
    if refresh:
        _refresh_market_reactions(db, force=force)

    rows = db.get_market_reactions()
    if not rows:
        raise click.ClickException("No market-reaction data — try with --refresh.")

    # Group by meeting for tidy printing.
    by_meeting: dict[dt.date, dict[str, MarketReaction]] = {}
    for r in rows:
        by_meeting.setdefault(r.meeting_date, {})[r.ticker] = r

    def _fmt(v: float | None, width: int = 6, prec: int = 2, sign: bool = True) -> str:
        if v is None:
            return "—".rjust(width)
        return (f"{v:+.{prec}f}" if sign else f"{v:.{prec}f}").rjust(width)

    click.echo(
        f"{'Meeting':<11}  {'Ticker':<5}  "
        f"{'Stmt Δ%':>7} {'σ':>5}  "
        f"{'EOD Δ%':>7} {'σ':>5}  "
        f"{'NextD Δ%':>8} {'σ':>5}  bars"
    )
    for md in sorted(by_meeting.keys(), reverse=True):
        for ticker in _REACTION_TICKERS:
            r = by_meeting[md].get(ticker)
            if r is None:
                continue
            click.echo(
                f"{md.isoformat():<11}  {ticker:<5}  "
                f"{_fmt(r.stmt_pct_change, 7)} {_fmt(r.stmt_realized_vol, 5, 1, False)}  "
                f"{_fmt(r.eod_pct_change, 7)} {_fmt(r.eod_realized_vol, 5, 1, False)}  "
                f"{_fmt(r.nextday_pct_change, 8)} {_fmt(r.nextday_realized_vol, 5, 1, False)}  "
                f"{r.bar_interval}"
            )


@cli.command("divergence")
@click.option("--asof", "asof_str", default=None,
              help="ISO date to compute divergence as-of (default: today).")
@click.option("--config", type=click.Path(path_type=Path), default=DEFAULT_CONFIG)
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
def divergence_cmd(asof_str: str | None, config: Path, db_path: Path) -> None:
    """Print committee divergence: spread, stdev, hawk/dove poles, camps.

    Useful for ad-hoc inspection or backtesting at past dates.
    """
    asof = dt.date.fromisoformat(asof_str) if asof_str else dt.date.today()
    speakers = load_speakers(config)
    db = Database(db_path)
    scores = db.all_scores()
    roster = [sp.key for sp in speakers if sp.key != "fomc"]

    snap = divergence_analysis.divergence_snapshot(scores, roster, asof)
    snap_30 = divergence_analysis.divergence_snapshot(
        scores, roster, asof - dt.timedelta(days=30)
    )

    click.echo(f"As of: {asof.isoformat()}  (window: trailing 90 days)")
    click.echo(f"Speakers covered: {snap.n_covered} / {snap.n_total}")
    click.echo(f"Spread (max − min): {snap.spread:+.3f}")
    click.echo(f"Stdev of speaker means: {snap.stdev:.3f}")
    if snap_30.n_covered > 0:
        delta = snap.spread - snap_30.spread
        click.echo(f"Spread vs 30 days ago: {delta:+.3f}")
    if snap.hawk_key:
        click.echo(f"Hawk pole: {snap.hawk_key}  Dove pole: {snap.dove_key}")
    click.echo()

    by_key = {sp.key: sp.name for sp in speakers}
    hawks, neutrals, doves = divergence_analysis.camps(snap)
    for label, camp in (("Hawks (>+0.3)", hawks),
                         ("Neutrals", neutrals),
                         ("Doves (<-0.3)", doves)):
        click.echo(f"{label}:")
        if not camp:
            click.echo("  (none)")
        for s in camp:
            name = by_key.get(s.speaker_key, s.speaker_key)
            click.echo(f"  {s.mean:+.2f}  {name}  (n={s.n})")
        click.echo()


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
    pw=None,
) -> list[AlertItem]:
    by_key = {sp.key: sp for sp in speakers}
    alerts: list[AlertItem] = []

    for ref in refs:
        speaker = by_key.get(ref.speaker_key)
        if speaker is None:
            log.warning("unknown speaker_key %s on %s; skipping", ref.speaker_key, ref.url)
            continue
        try:
            speech = fetch_speech(ref, pw=pw)
        except CaptionsUnavailable as exc:
            log.info("youtube captions unavailable: %s — %s", ref.url, exc)
            db.record_skip(
                ref.url, ref.speaker_key, ref.pub_date,
                "captions_unavailable", str(exc),
            )
            continue
        except Exception as exc:
            log.exception("fetch failed: %s — %s", ref.url, exc)
            db.record_skip(
                ref.url, ref.speaker_key, ref.pub_date,
                "fetch_failed", f"{type(exc).__name__}: {exc}"[:500],
            )
            continue

        canonical_url = db.insert_speech(StoredSpeech(
            url=speech.url,
            speaker_key=speech.speaker_key,
            speech_date=speech.speech_date,
            title=speech.title,
            location=speech.location,
            body=speech.body,
        ))
        if canonical_url != speech.url:
            log.info("dedup: %s -> canonical %s", speech.url, canonical_url)
            if db.has_score(canonical_url):
                continue

        filt = speech_filter.evaluate(speech.body, doc_type="speech")
        if not filt.passes:
            log.warning(
                "skip score: %s %s — %s (words=%d, kw=%d, link=%.2f)",
                speaker.key, speech.speech_date, filt.reason,
                filt.word_count, filt.keyword_hits, filt.link_density,
            )
            db.record_skip(
                canonical_url, speaker.key, speech.speech_date,
                "filter_rejected",
                f"{filt.reason} (words={filt.word_count}, "
                f"link_density={filt.link_density:.2f})",
            )
            continue

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

        if result.score is None:
            log.warning(
                "rubric excluded: %s %s — %s",
                speaker.key, speech.speech_date, result.rationale,
            )
            if db.delete_score(canonical_url):
                log.info("cleared stale score for excluded speech: %s", canonical_url)
            db.record_skip(
                canonical_url, speaker.key, speech.speech_date,
                "rubric_excluded", result.rationale[:500],
            )
            continue

        db.insert_score(
            speech_url=canonical_url,
            score=result.score,
            label=result.label,
            rationale=result.rationale,
            key_quotes=result.key_quotes,
            model=result.model,
            scored_at=result.scored_at,
        )
        db.clear_skip(canonical_url)
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
                speech_url=canonical_url,
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

        canonical_url = db.insert_speech(StoredSpeech(
            url=doc.url,
            speaker_key=doc.speaker_key,
            speech_date=doc.speech_date,
            title=doc.title,
            location="",
            body=doc.body,
            doc_type=doc.doc_type,
        ))
        if canonical_url != doc.url:
            log.info("dedup: %s -> canonical %s", doc.url, canonical_url)
            if db.has_score(canonical_url):
                continue

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

        if result.score is None:
            log.warning(
                "rubric excluded FOMC doc: %s %s — %s",
                doc.doc_type, doc.speech_date, result.rationale,
            )
            if db.delete_score(canonical_url):
                log.info("cleared stale score for excluded FOMC doc: %s", canonical_url)
            continue

        db.insert_score(
            speech_url=canonical_url,
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
                    db.set_diff_notes(canonical_url, notes)
                    log.info("annotated diff: %s -> %d notes", canonical_url, len(notes))
                except Exception as exc:
                    log.exception("annotate failed: %s — %s", canonical_url, exc)

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
                speech_url=canonical_url,
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
    ctx = _build_futures_context(db)
    reactions = db.get_market_reactions()
    governor_speakers = [sp for sp in speakers if sp.key != fomc_fetch.FOMC_SPEAKER_KEY]
    today = dt.date.today()
    stale = health.find_stale(governor_speakers, db.last_speech_dates(), today)
    for s in stale:
        if s.last_speech_date is None:
            log.warning("coverage health: %s (%s) has NO speeches stored",
                        s.speaker.name, s.speaker.region)
        else:
            log.warning("coverage health: %s (%s) silent for %d days (last: %s)",
                        s.speaker.name, s.speaker.region, s.days_silent,
                        s.last_speech_date.isoformat())
    skips = db.recent_skips(since=today - dt.timedelta(days=90))
    dash.render(
        speakers, scores, DEFAULT_DASHBOARD,
        futures_ctx=ctx, reactions=reactions, stale=stale, skips=skips,
    )
    log.info(
        "dashboard: regenerated at %s (%d documents, %d reactions, %d skips)",
        DEFAULT_DASHBOARD, len(scores), len(reactions), len(skips),
    )


def _build_futures_context(db: Database) -> FuturesContext | None:
    chain_rows = db.latest_chain()
    if not chain_rows:
        return None
    chain = {row[1]: row[4] for row in chain_rows}  # contract_month -> implied_rate
    settle_date = chain_rows[0][2]
    upcoming = db.upcoming_meetings(asof=dt.date.today(), limit=8)
    all_meetings = db.all_meetings()
    cur = futures_analysis.current_rate_from_chain(chain, all_meetings)
    return FuturesContext(
        chain=chain,
        chain_settle_date=settle_date,
        upcoming_meetings=upcoming,
        current_rate=cur,
    )


def _refresh_futures_and_calendar(db: Database) -> None:
    """Pull latest ZQ chain + refresh FOMC calendar (cached weekly)."""
    # Calendar: refresh if last fetch is older than 7 days, or never fetched.
    last = db.latest_calendar_fetch()
    stale = last is None or (
        dt.datetime.now(dt.timezone.utc) - last
    ) > dt.timedelta(days=7)
    if stale:
        try:
            meetings = fomc_calendar.fetch_meetings()
            for meeting_date, has_pc in meetings:
                db.upsert_meeting(meeting_date, has_pc)
            log.info("calendar: refreshed (%d meetings)", len(meetings))
        except Exception as exc:
            log.exception("calendar refresh failed: %s", exc)

    # Futures: pull current chain
    try:
        symbols = futures_fetch.chain_symbols(length=futures_fetch.CHAIN_LENGTH)
        settlements = futures_fetch.fetch_chain(symbols)
        for s in settlements:
            db.insert_settlement(
                s.contract_symbol, s.contract_month,
                s.settle_date, s.settle_price,
            )
        log.info("futures: fetched %d contracts", len(settlements))
    except Exception as exc:
        log.exception("futures fetch failed: %s", exc)


_REACTION_TICKERS: tuple[str, ...] = ("ES=F", "NQ=F", "ZT=F")


def _refresh_market_reactions(db: Database, *, force: bool = False) -> None:
    """Populate market_reactions for FOMC meetings that have a presser doc.

    Three windows per (meeting, ticker): statement (14:00->14:30 ET),
    same-day (14:30->16:00 ET), and next-day-close (14:30 ET -> next
    trading day 16:00 ET). Skips meetings whose next-day close hasn't
    yet happened.
    """
    now_utc = dt.datetime.now(dt.timezone.utc)
    fetched_at = now_utc

    pending_dates = db.meeting_dates_with_presser()
    refreshed = 0
    for meeting_date in pending_dates:
        stmt_dt, presser_dt = market_reaction_analysis.fomc_event_times(meeting_date)
        eod_dt, nextday_dt = market_reaction_analysis.cash_close_times(meeting_date)

        if nextday_dt > now_utc:
            log.info("market reactions: skipping %s (next-day close not yet reached)",
                     meeting_date)
            continue

        if not force and all(
            db.has_market_reaction(meeting_date, t) for t in _REACTION_TICKERS
        ):
            continue

        for ticker in _REACTION_TICKERS:
            if not force and db.has_market_reaction(meeting_date, ticker):
                continue
            try:
                bars = market_data.fetch_window(ticker, stmt_dt, nextday_dt)
            except Exception as exc:
                log.exception("market data fetch failed: %s %s — %s",
                              ticker, meeting_date, exc)
                continue
            if not bars.bars:
                log.info("market reactions: no bars for %s %s (interval=%s)",
                         ticker, meeting_date, bars.interval)
                continue

            stmt_m = market_reaction_analysis.compute_window(bars, stmt_dt, presser_dt)
            eod_m = market_reaction_analysis.compute_window(bars, presser_dt, eod_dt)
            nxt_m = market_reaction_analysis.compute_window(bars, presser_dt, nextday_dt)

            def _w(m, attr):
                return getattr(m, attr) if m else None

            db.insert_market_reaction(MarketReaction(
                meeting_date=meeting_date,
                ticker=ticker,
                statement_release_dt=stmt_dt,
                presser_start_dt=presser_dt,
                eod_close_dt=eod_dt,
                nextday_close_dt=nextday_dt,
                bar_interval=bars.interval,
                stmt_open=_w(stmt_m, "open"),
                stmt_close=_w(stmt_m, "close"),
                stmt_high=_w(stmt_m, "high"),
                stmt_low=_w(stmt_m, "low"),
                stmt_pct_change=_w(stmt_m, "pct_change"),
                stmt_realized_vol=_w(stmt_m, "realized_vol"),
                stmt_range_pct=_w(stmt_m, "range_pct"),
                stmt_max_move_pct=_w(stmt_m, "max_move_pct"),
                eod_open=_w(eod_m, "open"),
                eod_close=_w(eod_m, "close"),
                eod_high=_w(eod_m, "high"),
                eod_low=_w(eod_m, "low"),
                eod_pct_change=_w(eod_m, "pct_change"),
                eod_realized_vol=_w(eod_m, "realized_vol"),
                eod_range_pct=_w(eod_m, "range_pct"),
                eod_max_move_pct=_w(eod_m, "max_move_pct"),
                nextday_open=_w(nxt_m, "open"),
                nextday_close=_w(nxt_m, "close"),
                nextday_high=_w(nxt_m, "high"),
                nextday_low=_w(nxt_m, "low"),
                nextday_pct_change=_w(nxt_m, "pct_change"),
                nextday_realized_vol=_w(nxt_m, "realized_vol"),
                nextday_range_pct=_w(nxt_m, "range_pct"),
                nextday_max_move_pct=_w(nxt_m, "max_move_pct"),
                fetched_at=fetched_at,
            ))
            refreshed += 1
            log.info(
                "market reactions: %s %s -> stmt %s / eod %s / nextday %s (%s bars)",
                ticker, meeting_date,
                f"{stmt_m.pct_change:+.2f}%" if stmt_m else "—",
                f"{eod_m.pct_change:+.2f}%" if eod_m else "—",
                f"{nxt_m.pct_change:+.2f}%" if nxt_m else "—",
                bars.interval,
            )

    log.info("market reactions: %d rows refreshed", refreshed)


_REGIONAL_HOSTS: dict[str, str] = {
    "atlantafed.org":         "atlanta_bostic",
    "bostonfed.org":          "boston_collins",
    "chicagofed.org":         "chicago_goolsbee",
    "clevelandfed.org":       "cleveland_hammack",
    "dallasfed.org":          "dallas_logan",
    "kansascityfed.org":      "kc_schmid",
    "minneapolisfed.org":     "mpls_kashkari",
    "newyorkfed.org":         "ny_williams",
    "philadelphiafed.org":    "philly_paulson",
    "richmondfed.org":        "richmond_barkin",
    "stlouisfed.org":         "stl_musalem",
    "frbsf.org":              "sf_daly",
}


def _resolve_speaker_from_url(url: str, speakers: list[Speaker]) -> Speaker | None:
    """For FRB Board URLs (/newsevents/speech/<lastname>...) match aliases.
    For regional bank URLs, match by hostname.
    """
    lower = url.lower()
    for sp in speakers:
        if sp.source != "frb_board":
            continue
        for alias in sp.aliases:
            if f"/speech/{alias}" in lower:
                return sp

    by_key = {sp.key: sp for sp in speakers}
    for host, key in _REGIONAL_HOSTS.items():
        if host in lower:
            return by_key.get(key)
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


@cli.command("health")
@click.option("--threshold", "threshold_days", type=int,
              default=health.DEFAULT_THRESHOLD_DAYS,
              help="Flag speakers silent for more than this many days.")
@click.option("--config", type=click.Path(path_type=Path), default=DEFAULT_CONFIG)
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
def health_cmd(threshold_days: int, config: Path, db_path: Path) -> None:
    """List speakers whose latest stored speech is older than the threshold.

    A long quiet stretch can mean the scraper broke OR the speaker simply
    hasn't published a transcript-archived speech (TV/podcast appearances
    are intentionally excluded). Use this to spot which case is which.
    """
    speakers = load_speakers(config)
    governors = [sp for sp in speakers if sp.key != fomc_fetch.FOMC_SPEAKER_KEY]
    db = Database(db_path)
    stale = health.find_stale(
        governors, db.last_speech_dates(), dt.date.today(),
        threshold_days=threshold_days,
    )
    if not stale:
        click.echo(f"All {len(governors)} speakers fresh (threshold={threshold_days}d).")
        return
    click.echo(
        f"{len(stale)} of {len(governors)} speakers silent > {threshold_days}d:"
    )
    for s in stale:
        last = s.last_speech_date.isoformat() if s.last_speech_date else "never"
        days = "—" if s.last_speech_date is None else f"{s.days_silent}d"
        click.echo(f"  {days:>5}  {s.speaker.region:>12}  {s.speaker.name}  (last: {last})")


@cli.command("rescore")
@click.option("--since", "since_str", default=None,
              help="ISO date; only rescore speeches on/after this date.")
@click.option("--only", "only_keys", multiple=True,
              help="Restrict to one or more speaker_keys (repeatable).")
@click.option("--dry-run", is_flag=True, help="Print actions without modifying the DB.")
@click.option("--config", type=click.Path(path_type=Path), default=DEFAULT_CONFIG)
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
def rescore(
    since_str: str | None, only_keys: tuple[str, ...], dry_run: bool,
    config: Path, db_path: Path,
) -> None:
    """Re-evaluate existing speech bodies against the current rubric.

    Calls the Claude API for each row. Use after a rubric change to apply
    new scoring to historical speeches. Only doc_type='speech' rows are
    processed (FOMC docs bypass).
    """
    speakers = load_speakers(config)
    by_key = {sp.key: sp for sp in speakers}
    db = Database(db_path)

    where = ["s.doc_type = 'speech'"]
    params: list = []
    if since_str:
        where.append("s.speech_date >= ?")
        params.append(since_str)
    if only_keys:
        unknown = set(only_keys) - {sp.key for sp in speakers}
        if unknown:
            raise click.ClickException(f"Unknown speaker_key(s): {sorted(unknown)}")
        placeholders = ",".join("?" for _ in only_keys)
        where.append(f"s.speaker_key IN ({placeholders})")
        params.extend(only_keys)

    sql = f"""
        SELECT s.url, s.speaker_key, s.speech_date, s.title, s.body
        FROM speeches s
        JOIN speech_scores sc ON sc.speech_url = s.url
        WHERE {' AND '.join(where)}
        ORDER BY s.speech_date
    """
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    log.info("rescore: %d speech row(s) match", len(rows))
    rescored = 0
    excluded = 0
    skipped = 0
    for r in rows:
        speaker = by_key.get(r["speaker_key"])
        if speaker is None:
            log.warning("rescore: unknown speaker_key %s; skipping", r["speaker_key"])
            skipped += 1
            continue
        speech_date = dt.date.fromisoformat(r["speech_date"])
        if dry_run:
            click.echo(f"[dry-run] would rescore: {r['speaker_key']} {speech_date} {r['url']}")
            rescored += 1
            continue
        try:
            result = score_speech(
                speaker_name=speaker.name,
                speaker_role=speaker.role,
                speech_date=speech_date,
                title=r["title"],
                body=r["body"],
            )
        except Exception as exc:
            log.exception("rescore failed: %s — %s", r["url"], exc)
            skipped += 1
            continue
        if result.score is None:
            db.delete_score(r["url"])
            db.record_skip(
                r["url"], r["speaker_key"], speech_date,
                "rubric_excluded", result.rationale[:500],
            )
            click.echo(
                f"excluded: {r['speaker_key']} {speech_date} — {result.rationale}"
            )
            excluded += 1
        else:
            db.insert_score(
                speech_url=r["url"],
                score=result.score,
                label=result.label,
                rationale=result.rationale,
                key_quotes=result.key_quotes,
                model=result.model,
                scored_at=result.scored_at,
            )
            db.clear_skip(r["url"])
            click.echo(
                f"scored: {r['speaker_key']} {speech_date} -> "
                f"{result.score:+.2f} ({result.label})"
            )
            rescored += 1

    click.echo(
        f"{'[dry-run] ' if dry_run else ''}rescore summary: "
        f"{rescored} scored, {excluded} newly excluded, {skipped} skipped."
    )


@cli.command("clean")
@click.option("--dry-run", is_flag=True, help="Print actions without modifying the DB.")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=DEFAULT_DB)
def clean(dry_run: bool, db_path: Path) -> None:
    """Re-evaluate stored speeches; backfill content_hash; dedup URL duplicates;
    drop score rows for bodies that fail the speech-likeness filter.

    Speech bodies are preserved for audit (only `speech_scores` rows are deleted
    when the filter rejects). URL-duplicate `speeches` rows ARE removed.
    """
    db = Database(db_path)

    backfilled = 0
    url_dupes_removed = 0
    scores_dropped = 0

    with db.connect() as conn:
        # Pass 1: backfill content_hash for any row missing it.
        rows = conn.execute(
            "SELECT url, body FROM speeches WHERE content_hash IS NULL"
        ).fetchall()
        for r in rows:
            h = content_hash(r["body"])
            if dry_run:
                click.echo(f"[dry-run] backfill hash: {r['url']}")
            else:
                conn.execute(
                    "UPDATE speeches SET content_hash = ? WHERE url = ?",
                    (h, r["url"]),
                )
            backfilled += 1

        # Pass 2: resolve URL duplicates within the same (speaker, date, hash).
        # Compute hashes in Python rather than relying on the DB column so that
        # dry-run reports correctly even before backfill is applied.
        all_rows = conn.execute(
            "SELECT url, speaker_key, speech_date, body FROM speeches"
        ).fetchall()
        groups_map: dict[tuple, list[str]] = {}
        for r in all_rows:
            key = (r["speaker_key"], r["speech_date"], content_hash(r["body"]))
            groups_map.setdefault(key, []).append(r["url"])
        for (speaker_key, speech_date, _h), urls in groups_map.items():
            if len(urls) <= 1:
                continue
            g = {"speaker_key": speaker_key, "speech_date": speech_date}
            canonical = _pick_canonical_url(urls)
            removable = [u for u in urls if u != canonical]
            click.echo(
                f"{'[dry-run] ' if dry_run else ''}dedup {g['speaker_key']} "
                f"{g['speech_date']}: keeping {canonical}; removing {removable}"
            )
            if not dry_run:
                # If the canonical lacks a score but a duplicate has one, copy it over.
                canonical_score = conn.execute(
                    "SELECT 1 FROM speech_scores WHERE speech_url = ?", (canonical,),
                ).fetchone()
                if canonical_score is None:
                    for u in removable:
                        s = conn.execute(
                            "SELECT * FROM speech_scores WHERE speech_url = ?", (u,),
                        ).fetchone()
                        if s is not None:
                            conn.execute(
                                """INSERT OR REPLACE INTO speech_scores
                                   (speech_url, score, label, rationale, key_quotes,
                                    model, scored_at, diff_notes)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    canonical, s["score"], s["label"], s["rationale"],
                                    s["key_quotes"], s["model"], s["scored_at"],
                                    s["diff_notes"] if "diff_notes" in s.keys() else None,
                                ),
                            )
                            break
                placeholders = ",".join("?" for _ in removable)
                conn.execute(
                    f"DELETE FROM speech_scores WHERE speech_url IN ({placeholders})",
                    removable,
                )
                conn.execute(
                    f"DELETE FROM speeches WHERE url IN ({placeholders})",
                    removable,
                )
            url_dupes_removed += len(removable)

        # Pass 3: re-evaluate every speech-typed body against the new filter.
        rows = conn.execute(
            """SELECT s.url, s.speaker_key, s.speech_date, s.body
               FROM speeches s
               JOIN speech_scores sc ON sc.speech_url = s.url
               WHERE s.doc_type = 'speech'"""
        ).fetchall()
        for r in rows:
            filt = speech_filter.evaluate(r["body"], doc_type="speech")
            if filt.passes:
                continue
            click.echo(
                f"{'[dry-run] ' if dry_run else ''}drop score: {r['speaker_key']} "
                f"{r['speech_date']} {r['url']} — {filt.reason} "
                f"(words={filt.word_count}, kw={filt.keyword_hits}, "
                f"link={filt.link_density:.2f})"
            )
            if not dry_run:
                conn.execute(
                    "DELETE FROM speech_scores WHERE speech_url = ?", (r["url"],),
                )
                conn.execute(
                    """INSERT OR REPLACE INTO processing_skips
                       (url, speaker_key, pub_date, reason, message, recorded_at)
                       VALUES (?, ?, ?, 'filter_rejected', ?, ?)""",
                    (
                        r["url"], r["speaker_key"], r["speech_date"],
                        f"{filt.reason} (words={filt.word_count}, "
                        f"link_density={filt.link_density:.2f})",
                        dt.datetime.now(dt.timezone.utc).isoformat(),
                    ),
                )
            scores_dropped += 1

        # Pass 4: backfill skip records for speeches that have a body but no
        # score — these were excluded by the rubric in some prior run, but
        # the rationale wasn't preserved. Mark them generically so they
        # surface on the dashboard for transparency. Filter-rejected ones
        # take precedence (recorded above in Pass 3).
        skips_backfilled = 0
        rows = conn.execute(
            """SELECT s.url, s.speaker_key, s.speech_date, s.body
               FROM speeches s
               LEFT JOIN speech_scores sc ON sc.speech_url = s.url
               LEFT JOIN processing_skips ps ON ps.url = s.url
               WHERE s.doc_type = 'speech'
                 AND sc.speech_url IS NULL
                 AND ps.url IS NULL"""
        ).fetchall()
        for r in rows:
            filt = speech_filter.evaluate(r["body"], doc_type="speech")
            reason = "filter_rejected" if not filt.passes else "rubric_excluded"
            message = (
                f"{filt.reason} (words={filt.word_count})"
                if not filt.passes
                else "previously excluded; original rationale not preserved"
            )
            click.echo(
                f"{'[dry-run] ' if dry_run else ''}backfill skip: "
                f"{r['speaker_key']} {r['speech_date']} — {reason}"
            )
            if not dry_run:
                conn.execute(
                    """INSERT OR REPLACE INTO processing_skips
                       (url, speaker_key, pub_date, reason, message, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        r["url"], r["speaker_key"], r["speech_date"],
                        reason, message,
                        dt.datetime.now(dt.timezone.utc).isoformat(),
                    ),
                )
            skips_backfilled += 1

    summary = (
        f"{'[dry-run] ' if dry_run else ''}clean summary: "
        f"backfilled {backfilled} hash(es), "
        f"removed {url_dupes_removed} URL duplicate(s), "
        f"dropped {scores_dropped} score row(s), "
        f"backfilled {skips_backfilled} skip record(s)."
    )
    click.echo(summary)


def _pick_canonical_url(urls: list[str]) -> str:
    """Prefer URLs that contain a 4-digit year segment (e.g. .../2026/sp-...);
    otherwise pick the lexicographically smallest URL for determinism."""
    import re as _re
    year_re = _re.compile(r"/(19|20)\d{2}/")
    with_year = [u for u in urls if year_re.search(u)]
    pool = with_year if with_year else urls
    return sorted(pool)[0]


if __name__ == "__main__":
    cli()
