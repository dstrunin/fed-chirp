# Fed Chirp

Monitors Federal Reserve Board governor speeches, scores each one for hawkish/dovish tone using the Claude API, and reports tone shifts.

## What it does

1. Pulls new speeches from each Board governor's RSS feed on federalreserve.gov.
2. Scores each speech on a -2 (very dovish) to +2 (very hawkish) scale via Claude.
3. Stores everything in SQLite.
4. Regenerates a local HTML dashboard with per-member sparklines and a heatmap.
5. Emails an alert when a speech deviates >=1.0 from the speaker's trailing 90-day mean (or |z|>=1.5).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env   # then fill in the values
```

## Usage

```bash
fed-chirp scan                          # cron entry: discover, fetch, score, alert
fed-chirp backfill --since 2026-01-01   # re-scan all Board speeches since date
fed-chirp dashboard                     # regenerate dashboard/index.html only
fed-chirp score-one <speech-url>        # debug: score a single URL
fed-chirp scan --dry-run                # don't send email
```

Open `dashboard/index.html` in any browser to see the big-picture view.

## Cron

Copy `launchd/com.user.fedchirp.plist` into `~/Library/LaunchAgents/` and load it with `launchctl load`. Runs Mon-Fri at 18:30 local.
