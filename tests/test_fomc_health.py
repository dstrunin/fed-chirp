"""FOMC completeness regressions; synthetic databases, no network/model calls."""
import datetime as dt
from contextlib import nullcontext

import pytest
from bs4 import BeautifulSoup
from click.testing import CliRunner

from fed_chirp import cli
from fed_chirp.analysis import health
from fed_chirp.fetchers import fomc
from fed_chirp.output import dashboard
from fed_chirp.storage.db import Database, StoredSpeech

DAY = dt.date(2026, 10, 28)
NOW = dt.datetime(2026, 10, 31, 12, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def fixed_health_clock(monkeypatch):
    real_check = health.check_fomc
    def fixed(*args, **kwargs):
        kwargs.setdefault("now", NOW)
        return real_check(*args, **kwargs)
    # Tests must remain stable after the fixture calendar has expired.
    monkeypatch.setattr(health, "check_fomc", fixed)


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    db.upsert_meeting(DAY, True)
    db.upsert_meeting(dt.date(2026, 12, 9), True)
    return db


def ref(kind=fomc.DOC_STATEMENT, date=DAY):
    return fomc.FomcRef(f"https://example.com/{date}/{kind}", kind,
                        "powell" if kind == fomc.DOC_PRESSER else "fomc", date, kind)


def store(db, r, scored=False):
    db.insert_speech(StoredSpeech(r.url, r.speaker_key, r.pub_date, r.title, "",
                                  r.title + r.url, r.doc_type))
    if scored:
        db.insert_score(r.url, 0.5, "hawkish", "test", [], "test", NOW)


def check(db, refs=(), now=NOW):
    return health.check_fomc(db, refs, now=now)


def complete(db):
    for kind in (fomc.DOC_STATEMENT, fomc.DOC_PRESSER):
        store(db, ref(kind), scored=True)


def test_calendar_detects_entire_missing_latest_meeting(db):
    report = check(db)
    assert not report.ok
    assert report.latest_expected == DAY
    assert any("2026-10-28" in s and "Statement" in s and "missing" in s for s in report.issues)
    assert any("Press Conference" in s and "missing" in s for s in report.issues)


def test_stored_unscored_statement_is_not_hidden_by_previous_scores(db):
    store(db, ref(date=dt.date(2026, 9, 16)), scored=True)
    store(db, ref())
    report = check(db)
    assert any("Statement" in s and "unscored" in s for s in report.issues)


def test_complete_latest_is_healthy_and_speech_exclusions_do_not_count(db):
    complete(db)
    speech = fomc.FomcRef("https://example.com/speech", "speech", "powell", DAY, "speech")
    store(db, speech)
    db.record_skip(speech.url, "powell", DAY, "rubric_excluded", "non-policy")
    assert check(db, [speech]).ok


def test_discovered_fetch_failure_is_visible_without_stored_body(db):
    complete(db)
    minutes = ref(fomc.DOC_MINUTES)
    report = check(db, [minutes])
    assert not report.ok
    assert any(minutes.url in s and "not stored" in s for s in report.issues)


def test_discovered_alternate_url_accepts_scored_meeting_document(db):
    complete(db)
    alternate = ref()
    alternate.url += "?alternate"
    assert check(db, [alternate]).ok


@pytest.mark.parametrize("now,expected", [
    (dt.datetime(2026, 10, 28, 21, 59, tzinfo=dt.timezone.utc), None),
    (dt.datetime(2026, 10, 28, 22, 0, tzinfo=dt.timezone.utc), DAY),
    (dt.datetime(2026, 10, 29, 0, 1, tzinfo=dt.timezone.utc), DAY),
])
def test_calendar_grace_is_1800_eastern_not_midnight_or_utc(db, now, expected):
    report = check(db, now=now)
    assert report.latest_expected == expected
    assert report.ok == (expected is None)


def test_winter_deadline_uses_est(db):
    winter = dt.date(2026, 12, 9)
    before = check(db, now=dt.datetime(2026, 12, 9, 22, 59, tzinfo=dt.timezone.utc))
    after = check(db, now=dt.datetime(2026, 12, 9, 23, 0, tzinfo=dt.timezone.utc))
    assert before.latest_expected == DAY
    assert after.latest_expected == winter


def test_no_presser_expected_when_calendar_says_no(db):
    db.upsert_meeting(DAY, False)
    store(db, ref(), scored=True)
    assert check(db).ok


def test_calendar_unavailable_or_exhausted_is_not_healthy(tmp_path, db):
    assert not check(Database(tmp_path / "empty.sqlite")).ok
    assert not check(db, now=dt.datetime(2027, 2, 1, tzinfo=dt.timezone.utc)).ok


def test_dashboard_warns_and_does_not_call_previous_score_current(db, tmp_path):
    store(db, ref(date=dt.date(2026, 9, 16)), scored=True)
    out = tmp_path / "index.html"
    dashboard.render([], db.all_scores(), out, fomc_health=check(db))
    page = BeautifulSoup(out.read_text(), "html.parser")
    warning = page.select_one('[role="alert"]')
    assert warning is not None
    assert "2026-10-28" in warning.get_text()
    assert "unavailable" in page.select_one(".reading").get_text().lower()
    assert "combined" not in page.select_one(".reading").get_text().lower()
    latest = page.select_one(".fomc-cell--latest")
    assert "2026–10–28" in latest.get_text()
    assert latest.select_one(".combined").get_text() == "—"


def test_partial_meeting_never_presents_single_score_as_combined(db, tmp_path):
    store(db, ref(), scored=True)
    out = tmp_path / "index.html"
    dashboard.render([], db.all_scores(), out, fomc_health=check(db))
    page = BeautifulSoup(out.read_text(), "html.parser")
    assert page.select_one(".fomc-cell--latest .combined").get_text() == "—"


def scan_setup(monkeypatch, db, tmp_path):
    monkeypatch.setattr(cli, "Database", lambda path: db)
    monkeypatch.setattr(cli, "load_speakers", lambda path: [])
    monkeypatch.setattr(cli, "maybe_playwright", lambda speakers: nullcontext(None))
    monkeypatch.setattr(cli, "discover", lambda *a, **k: [])
    monkeypatch.setattr(cli.fomc_fetch, "discover", lambda **k: [ref(), ref(fomc.DOC_PRESSER)])
    monkeypatch.setattr(cli, "_refresh_futures_and_calendar", lambda db: None)
    monkeypatch.setattr(cli, "_refresh_market_reactions", lambda db: None)
    monkeypatch.setattr(cli, "_build_futures_context", lambda db: None)
    monkeypatch.setattr(cli, "DEFAULT_DASHBOARD", tmp_path / "index.html")
    monkeypatch.setattr(cli, "send_alerts", lambda *a, **k: None)
    monkeypatch.setattr(cli.fomc_fetch, "fetch_doc", lambda r: fomc.FomcDoc(
        r.url, r.doc_type, r.speaker_key, r.pub_date, r.title, r.url))


def test_scan_scoring_failure_exits_nonzero_after_render_and_summary(monkeypatch, db, tmp_path):
    scan_setup(monkeypatch, db, tmp_path)
    def fail(**kwargs):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(cli, "score_speech", fail)
    summary = tmp_path / "summary.md"
    result = CliRunner().invoke(cli.cli, ["scan", "--dry-run", "--summary-file", str(summary)])
    assert result.exit_code == 2
    assert "FOMC" in result.output
    assert (tmp_path / "index.html").exists()
    assert "FOMC completeness" in summary.read_text()
    assert "unscored" in summary.read_text()


def test_speech_pipeline_failure_cannot_prevent_fomc_processing(monkeypatch, db, tmp_path):
    scan_setup(monkeypatch, db, tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("speech pipeline unavailable")
    monkeypatch.setattr(cli, "_process_speeches", fail)
    from fed_chirp.scoring.scorer import ScoreResult
    monkeypatch.setattr(cli, "score_speech", lambda **k: ScoreResult(0.5, "hawkish", "test", [], "test", NOW))
    result = CliRunner().invoke(cli.cli, ["scan", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert db.has_score(ref().url)
    assert db.has_score(ref(fomc.DOC_PRESSER).url)
    assert (tmp_path / "index.html").exists()


def test_standalone_fomc_health_exit_codes(monkeypatch, db):
    result = CliRunner().invoke(cli.cli, ["fomc-health", "--db", str(db.path)])
    assert result.exit_code == 1
    assert "2026-10-28" in result.output
    complete(db)
    result = CliRunner().invoke(cli.cli, ["fomc-health", "--db", str(db.path)])
    assert result.exit_code == 0, result.output
    assert "healthy" in result.output.lower()


def test_fomc_only_skips_all_speech_discovery(monkeypatch, db, tmp_path):
    scan_setup(monkeypatch, db, tmp_path)
    complete(db)
    def forbidden(*args, **kwargs):
        pytest.fail("speech discovery/browser must not run")
    monkeypatch.setattr(cli, "discover", forbidden)
    monkeypatch.setattr(cli, "maybe_playwright", forbidden)
    result = CliRunner().invoke(cli.cli, ["scan", "--dry-run", "--fomc-only"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "index.html").exists()


def test_unexpected_render_abort_never_uses_publishable_exit_two(monkeypatch, db, tmp_path):
    scan_setup(monkeypatch, db, tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("render failed")
    monkeypatch.setattr(cli, "_regenerate_dashboard", fail)
    monkeypatch.setattr(cli, "_process_fomc", lambda *a, **k: [])
    result = CliRunner().invoke(cli.cli, ["scan", "--dry-run"])
    assert result.exit_code == 1
    assert not (tmp_path / "index.html").exists()


def test_health_command_does_not_create_a_missing_database(tmp_path):
    missing = tmp_path / "missing.sqlite"
    result = CliRunner().invoke(cli.cli, ["fomc-health", "--db", str(missing)])
    assert result.exit_code == 1
    assert not missing.exists()


def test_health_command_does_not_write_existing_database(db):
    complete(db)
    before = db.path.stat().st_mtime_ns, db.path.read_bytes()
    result = CliRunner().invoke(cli.cli, ["fomc-health", "--db", str(db.path)])
    assert result.exit_code == 0, result.output
    assert (db.path.stat().st_mtime_ns, db.path.read_bytes()) == before


@pytest.mark.parametrize("failure", ["fetch", "excluded", "discovery"])
def test_scan_fomc_failures_render_warning_before_exit_two(monkeypatch, db, tmp_path, failure):
    scan_setup(monkeypatch, db, tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("offline")
    if failure == "fetch":
        monkeypatch.setattr(cli.fomc_fetch, "fetch_doc", fail)
    elif failure == "discovery":
        complete(db)  # Fresh stored scores cannot conceal discovery failure.
        monkeypatch.setattr(cli.fomc_fetch, "discover", fail)
    else:
        from fed_chirp.scoring.scorer import ScoreResult
        monkeypatch.setattr(cli, "score_speech", lambda **k: ScoreResult(
            None, "excluded", "invalid document", [], "test", NOW))
    result = CliRunner().invoke(cli.cli, ["scan", "--dry-run"])
    assert result.exit_code == 2, result.output
    page = BeautifulSoup((tmp_path / "index.html").read_text(), "html.parser")
    assert page.select_one('[role="alert"]') is not None


def test_speech_discovery_failure_does_not_block_fomc(monkeypatch, db, tmp_path):
    scan_setup(monkeypatch, db, tmp_path)
    complete(db)
    def fail(*args, **kwargs):
        raise RuntimeError("speech feed offline")
    monkeypatch.setattr(cli, "discover", fail)
    result = CliRunner().invoke(cli.cli, ["scan", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "index.html").exists()


def test_presser_scoring_uses_generic_chair_name(monkeypatch, db, tmp_path):
    scan_setup(monkeypatch, db, tmp_path)
    names = []
    from fed_chirp.scoring.scorer import ScoreResult
    def score(**kwargs):
        names.append(kwargs["speaker_name"])
        return ScoreResult(0.5, "hawkish", "test", [], "test", NOW)
    monkeypatch.setattr(cli, "score_speech", score)
    cli._process_fomc([ref(fomc.DOC_PRESSER)], db)
    assert names == ["FOMC Chair"]
