#!/usr/bin/env python3
"""Read-only stdlib watchdog. Healthy is silent; unresolved alerts repeat.

Staleness counts weekday hours in America/New_York, excluding weekends (and
respecting DST). Running age always uses wall-clock hours, including weekends.
Exit 1 and one stdout line indicate failure/staleness/missing or invalid state.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


def parse_timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return parsed.astimezone(timezone.utc)


def weekday_seconds(start, end, zone):
    """Count elapsed seconds in weekday date intervals, not nominal DST days."""
    total = 0.0
    cursor = start
    while cursor < end:
        local = cursor.astimezone(zone)
        midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=zone)
        boundary = min(midnight.astimezone(timezone.utc), end)
        if local.weekday() < 5:
            total += (boundary - cursor).total_seconds()
        cursor = boundary
    return total


def alert(status, now, zone):
    if not isinstance(status, dict) or status.get("status") not in ("running", "success", "failed"):
        raise ValueError("invalid status")
    # Reject future/naive/malformed fields before accepting a healthy state.
    for key in ("started_at", "updated_at", "finished_at", "verified_at", "last_verified_success"):
        value = status.get(key)
        if value is not None and parse_timestamp(value) > now:
            raise ValueError("future timestamp")
    if status["status"] == "failed":
        # No raw exception/command output is produced by the publisher.
        error = " ".join(str(status.get("error") or "publisher failed").split())[:240]
        return f"failed at {status.get('stage', 'unknown')}: {error}"
    if status["status"] == "running":
        started = parse_timestamp(status["started_at"])
        if (now - started).total_seconds() > 2 * 3600:
            return f"in progress over 2h at {status.get('stage', 'unknown')}"
        return None
    last = parse_timestamp(status["last_verified_success"])
    if status.get("stage") != "verified":
        raise ValueError("success without verification")
    if weekday_seconds(last, now, zone) > 30 * 3600:
        return "stale: no verified success for over 30 weekday hours"
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(os.environ.get("FED_CHIRP_REPO", Path(__file__).resolve().parents[1]))
    state_dir = Path(os.environ.get("FED_CHIRP_STATE_DIR", root / ".publish-state"))
    parser.add_argument("--status", type=Path, default=state_dir / "status.json")
    parser.add_argument("--now", help="ISO timestamp override for deterministic checks")
    parser.add_argument("--timezone", default="America/New_York")
    args = parser.parse_args(argv)
    try:
        now = parse_timestamp(args.now) if args.now else datetime.now(timezone.utc)
        message = alert(json.loads(args.status.read_text()), now, ZoneInfo(args.timezone))
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        message = "missing or invalid publisher status"
    if message:
        print(f"Fed Chirp: {message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
