# Fed Chirp

Personal monitor of Federal Reserve communications. Scrapes Board governor speeches and FOMC documents, scores each one for hawkish/dovish tone via local Hermes/Codex auth, and reports tone shifts as alerts and a local dashboard.

## What it covers

- **Board governor speeches** — all 7 sitting governors (Powell, Jefferson, Bowman, Barr, Cook, Miran, Waller), discovered via per-speaker RSS feeds on federalreserve.gov.
- **Regional Fed bank presidents** — all 12 (Williams/NY, Daly/SF, Goolsbee/Chicago, Kashkari/Mpls, Bostic/Atlanta, Logan/Dallas, Hammack/Cleveland, Schmid/KC, Collins/Boston, Paulson/Philly, Barkin/Richmond, Musalem/St. Louis), each with their own bank-specific scraper (RSS, HTML index, or headless Chromium depending on what the bank publishes).
- **FOMC statements** — the canonical post-meeting policy text (~8/yr).
- **FOMC minutes** — the deliberation record released ~3 weeks after each meeting.
- **FOMC Chair press conferences** — same-day Q&A transcripts (PDF, parsed via `pypdf`).

## What it does

1. **Discovers** new docs from federalreserve.gov RSS feeds (per-speaker for speeches; the consolidated `press_monetary` feed for FOMC docs). Press-conference URLs are derived from each statement's date and probed with HEAD.
2. **Scores** each doc on a -2 (very dovish) to +2 (very hawkish) scale via Hermes, using the locally configured Codex OAuth provider by default. The prompt header switches per doc-type so the model knows it's reading a speech vs a statement vs minutes vs a press-conference transcript.
3. **Annotates** new FOMC statements with 3-5 bullet notes explaining what specific wording changed vs the previous statement and what each shift signals (separate Hermes call).
4. **Persists** everything in SQLite at `data/fed_chirp.sqlite`, keyed on URL with a `doc_type` discriminator.
5. **Analyzes** for tone shifts:
   - *Speeches*: alert when |score − speaker's 90-day mean| ≥ 1.0 or |z| ≥ 1.5.
   - *FOMC docs*: alert when |score − prior doc of same type| ≥ 0.5 (8/yr cadence makes a 90d baseline too sparse).
6. **Renders** a local HTML dashboard (regenerated each scan) with:
   - **FOMC pulse** — per-meeting combined view (statement + presser as one event), with intra-meeting drift (presser − statement), meeting-over-meeting Δ, and minutes when available; per-doc-type tables below; expandable diff notes under each statement.
   - **Board governors** — per-member 90-day mean, sparkline of last 30 speeches, most recent score.
   - **Recent speeches** — last 30 with rationale and link.
7. **Emails** an HTML digest when alerts fire, with diff and explanatory notes inlined for FOMC statement alerts.

## Disclaimer

Fed Chirp is an independent personal project and is not affiliated with, endorsed by, or representative of the Federal Reserve System, any Federal Reserve Bank, the Federal Open Market Committee, or any other government body. The "Fed Chirp" name and any aesthetic resemblance to official Federal Reserve material are coincidental — no claim of affiliation is made or implied.

Hawk/dove scores shown in this project are **interpretations produced by a large language model** via Hermes against the fixed rubric in [`src/fed_chirp/scoring/prompt.py`](src/fed_chirp/scoring/prompt.py). They reflect that rubric's reading of each document — not the speaker's intent, not any official position, and not consensus economic analysis. The rubric can be wrong; the model can be wrong; the scraped source can be wrong or incomplete. Treat every number as a heuristic.

**Nothing in this repository or on the dashboard is investment, financial, legal, tax, or other professional advice.** Do not use these scores to make trading or policy decisions on their own.

Coverage depends on best-effort web scraping of public sources (federalreserve.gov RSS, each regional Reserve Bank's website, YouTube auto-captions for some video appearances). Sources occasionally change layout, restrict access, or stop publishing; data may therefore be missing, stale, or duplicated.

Code and data are provided as-is with no warranty.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium    # one-time: 4 of the 12 regional banks (Chicago,
                               # Cleveland, KC, Dallas) plus St. Louis and a
                               # couple others render their speech listings
                               # client-side, so we use headless Chrome to
                               # extract them. PlaywrightContext prefers your
                               # installed Chrome, falling back to the bundled
                               # chromium-headless-shell.
cp .env.example .env   # fill in Gmail settings; Hermes defaults use local Codex auth
```

Fed Chirp shells out to `hermes chat -Q --provider openai-codex -m gpt-6-astra --source tool` for scoring. Override with `FED_CHIRP_HERMES_BIN`, `FED_CHIRP_HERMES_PROVIDER`, `FED_CHIRP_HERMES_MODEL`, or `FED_CHIRP_HERMES_TIMEOUT` if your local Hermes setup differs.

## Usage

```bash
fed-chirp scan                          # cron entry: discover speeches + FOMC docs, score, alert
fed-chirp backfill --since 2026-01-01   # one-time bulk fetch of everything since date
fed-chirp backfill --since 2025-11-08 --only ny_williams   # restrict to one bank
fed-chirp dashboard                     # regenerate dashboard/index.html from existing data
fed-chirp diff <statement-url>          # word-diff vs prior statement + auto-generated notes
fed-chirp annotate-diffs                # backfill diff notes for any statements missing them
fed-chirp score-one <url>               # debug: fetch + score a single URL (auto-detects doc type)
fed-chirp scan --dry-run                # print would-be emails to stdout instead of sending
```

Open `dashboard/index.html` in any browser to see the big-picture view.

## Cron

Production uses **Hermes script-only cron**, weekdays at **18:30 America/New_York**, on the authenticated Mac. Do not also load the legacy launchd template: that creates duplicate schedulers and can hit Desktop-folder privacy restrictions.

- Scheduler launcher: `~/.hermes/scripts/fed_chirp_scan_publish.sh`.
- Versioned runner: `scripts/scan_and_publish.sh`; logs: `data/fedchirp.cron.log`.
- The launcher is detached to respect the scheduler's short runtime limit. Its return means **started**, not published. The runner's atomic status file and watchdog are the completion authority.
- A separate script-only watchdog checks completion and alerts on failures, stuck runs or stale publication; healthy checks are silent. These checks make no LLM calls.
- Scoring still requires valid local Codex OAuth. A model retirement, CLI incompatibility or network outage cannot be prevented, but must not silently masquerade as a successful update.

### Recovery and verification

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m fed_chirp.cli fomc-health
.venv/bin/python -m fed_chirp.cli scan --fomc-only --dry-run
scripts/scan_and_publish.sh
```

FOMC health is independent of ordinary speech coverage. It checks expected meeting documents against the official calendar and actual saved scores, shows a visible dashboard warning when incomplete, and makes scans return nonzero after regenerating the warning. Expected meeting documents become due at 18:00 ET on the meeting date (already discovered or stored documents are checked immediately). A short/empty feed must not make an already-completed meeting disappear.

The publisher permits dirty generated HTML/database files left by an interrupted run, but refuses unrelated tracked or untracked source changes. If upstream has advanced, it first saves both the working and staged artifacts under `.publish-state/backups/artifacts-*/` (including commit metadata), then syncs and regenerates from the upstream data. Backups survive failures and are never automatically restored over newer data. It uses a process-owned lock, retries unpushed commits even when there is no new generated diff, and verifies the public application HTML before recording success (allowing only the exact known Cloudflare challenge and site-specific analytics script structures). Unexpected scan failures are not published; explicit FOMC-incomplete results publish the warning while remaining failures.

Regression coverage includes Hermes session metadata on either side of JSON, CLI invocation defaults, FOMC completeness/grace boundaries, publisher failure/retry behavior and the watchdog. Press-conference scoring preserves the opening PDF page and does not hardcode the Chair's identity.

## Cost / execution model

Scoring is local-first: Fed Chirp calls your local Hermes CLI, which can use Codex OAuth instead of a paid project API key. Scheduled scans run on the Mac where Hermes is authenticated. GitHub Actions runs regression tests only; it does not need local Codex OAuth and does not duplicate the production scanner.

## Roadmap

- SEP / dot-plot tracking — quarterly numeric projections.
- FRASER historical backfill — pre-RSS speeches for longer baselines.
- Regional bank coverage gaps:
  - Chicago has no public archive (only the latest speech is reachable via the homepage).
  - Some St. Louis & Mpls "remarks" pages are short Q&A descriptions rather than full transcripts.
  - **Pagination needed** — these listings expose only a recent window, so historical coverage is shallow despite long tenures: SF Daly (only since 2024-10 vs tenure 2018-10), Atlanta Bostic (2023-06 vs 2017-06), Mpls Kashkari (2025-04 vs 2016-01), KC Schmid (2026-03 vs 2023-08). Each parser needs to walk pagination (FacetWP for SF, RSS archive for Atlanta, paginated profile for Mpls, listing pages for KC) and then a one-shot `fed-chirp backfill --since <tenure_start> --only <key>` run.
