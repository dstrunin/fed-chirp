# Fed Chirp

Personal monitor of Federal Reserve communications. Scrapes Board governor speeches and FOMC documents, scores each one for hawkish/dovish tone via the Claude API, and reports tone shifts as alerts and a local dashboard.

## What it covers

- **Board governor speeches** — all 7 sitting governors (Powell, Jefferson, Bowman, Barr, Cook, Miran, Waller), discovered via per-speaker RSS feeds on federalreserve.gov.
- **FOMC statements** — the canonical post-meeting policy text (~8/yr).
- **FOMC minutes** — the deliberation record released ~3 weeks after each meeting.
- **Powell press conferences** — same-day Q&A transcripts (PDF, parsed via `pypdf`).

## What it does

1. **Discovers** new docs from federalreserve.gov RSS feeds (per-speaker for speeches; the consolidated `press_monetary` feed for FOMC docs). Press-conference URLs are derived from each statement's date and probed with HEAD.
2. **Scores** each doc on a -2 (very dovish) to +2 (very hawkish) scale via Claude Sonnet 4.6 with prompt caching on the rubric. The user-message header switches per doc-type so the model knows it's reading a speech vs a statement vs minutes vs a press-conference transcript.
3. **Annotates** new FOMC statements with 3-5 bullet notes explaining what specific wording changed vs the previous statement and what each shift signals (separate Claude call).
4. **Persists** everything in SQLite at `data/fed_chirp.sqlite`, keyed on URL with a `doc_type` discriminator.
5. **Analyzes** for tone shifts:
   - *Speeches*: alert when |score − speaker's 90-day mean| ≥ 1.0 or |z| ≥ 1.5.
   - *FOMC docs*: alert when |score − prior doc of same type| ≥ 0.5 (8/yr cadence makes a 90d baseline too sparse).
6. **Renders** a local HTML dashboard (regenerated each scan) with:
   - **FOMC pulse** — per-meeting combined view (statement + presser as one event), with intra-meeting drift (presser − statement), meeting-over-meeting Δ, and minutes when available; per-doc-type tables below; expandable diff notes under each statement.
   - **Board governors** — per-member 90-day mean, sparkline of last 30 speeches, most recent score.
   - **Recent speeches** — last 30 with rationale and link.
7. **Emails** an HTML digest when alerts fire, with diff and explanatory notes inlined for FOMC statement alerts.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env   # fill in ANTHROPIC_API_KEY, GMAIL_APP_PASSWORD, etc.
```

## Usage

```bash
fed-chirp scan                          # cron entry: discover speeches + FOMC docs, score, alert
fed-chirp backfill --since 2026-01-01   # one-time bulk fetch of everything since date
fed-chirp dashboard                     # regenerate dashboard/index.html from existing data
fed-chirp diff <statement-url>          # word-diff vs prior statement + auto-generated notes
fed-chirp annotate-diffs                # backfill diff notes for any statements missing them
fed-chirp score-one <url>               # debug: fetch + score a single URL (auto-detects doc type)
fed-chirp scan --dry-run                # print would-be emails to stdout instead of sending
```

Open `dashboard/index.html` in any browser to see the big-picture view.

## Cron

Copy `launchd/com.user.fedchirp.plist` into `~/Library/LaunchAgents/` and load it with `launchctl load`. Runs Mon-Fri at 18:30 local.

## Cost

Steady-state Claude API spend is roughly **$5-10/year**: dominated by speech bodies (governor speeches scored at ~$0.016/each, ~5-10 new speeches/week). FOMC docs add ~$1.50/year. Diff-note annotation adds another ~$0.10/year. Prompt caching keeps the rubric warm across each scan.

## Roadmap

- Regional Fed bank presidents (Williams, Daly, Kashkari, Bostic, Goolsbee, Logan, etc.) — broadens to the full FOMC voting voice base.
- Fed funds futures comparison — derive market-implied rate path and surface "is the Fed saying something different from what's priced in?"
- SEP / dot-plot tracking — quarterly numeric projections.
- FRASER historical backfill — pre-RSS speeches for longer baselines.
