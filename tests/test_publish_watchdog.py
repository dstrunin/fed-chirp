"""Deterministic watchdog boundaries; no network and no production scan."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/publish_watchdog.py"


def check(tmp_path, status, now="2026-11-02T13:00:00+00:00"):
    path = tmp_path / "status.json"
    if status is not None:
        path.write_text(json.dumps(status))
    return subprocess.run([sys.executable, str(SCRIPT), "--status", str(path), "--now", now],
                          text=True, capture_output=True, timeout=5)


def success(at="2026-11-02T10:00:00+00:00"):
    return dict(status="success", stage="verified", updated_at=at, finished_at=at,
                last_verified_success=at, error=None, sha="abc")


def test_healthy_is_silent(tmp_path):
    result = check(tmp_path, success())
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


def test_failure_is_repeated_until_resolved(tmp_path):
    status = dict(success(), status="failed", error="scan exited 2")
    for _ in range(2):
        result = check(tmp_path, status)
        assert result.returncode == 1
        assert "scan exited 2" in result.stdout
        assert result.stderr == ""
        assert len(result.stdout.splitlines()) == 1


@pytest.mark.parametrize("status", [None, {}, {"status": "other"}, dict(success(), last_verified_success="bad")])
def test_missing_or_malformed_status_alerts(tmp_path, status):
    result = check(tmp_path, status)
    assert result.returncode == 1
    assert result.stdout.startswith("Fed Chirp:")
    assert result.stderr == ""


def test_in_progress_under_two_hours_silent(tmp_path):
    result = check(tmp_path, dict(success(), status="running", started_at="2026-11-02T12:00:00+00:00"))
    assert result.returncode == 0
    assert result.stdout == ""


def test_in_progress_over_two_hours_alerts_even_with_recent_update(tmp_path):
    result = check(tmp_path, dict(success(), status="running", started_at="2026-11-02T10:59:59+00:00",
                                 updated_at="2026-11-02T12:59:00+00:00"))
    assert result.returncode == 1
    assert "in progress" in result.stdout


@pytest.mark.parametrize("at,expected", [("2026-11-03T16:00:00+00:00", 0),
                                         ("2026-11-03T16:00:01+00:00", 1)])
def test_thirty_hour_weekday_boundary(tmp_path, at, expected):
    result = check(tmp_path, success(), now=at)
    assert result.returncode == expected
    assert bool(result.stdout) == bool(expected)


def test_weekend_hours_do_not_count_as_staleness(tmp_path):
    result = check(tmp_path, success("2026-10-30T20:00:00+00:00"), now="2026-11-02T20:00:00+00:00")
    assert result.returncode == 0
    assert result.stdout == ""
    overdue = check(tmp_path, success("2026-10-30T20:00:00+00:00"), now="2026-11-03T03:00:01+00:00")
    assert overdue.returncode == 1
    assert "stale" in overdue.stdout


def test_future_timestamp_fails_closed(tmp_path):
    result = check(tmp_path, success("2026-11-04T10:00:00+00:00"))
    assert result.returncode == 1
