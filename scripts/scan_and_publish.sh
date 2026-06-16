#!/bin/bash
set -euo pipefail

REPO="/Users/dima/Desktop/Archives/fed_chirp"
LOG_DIR="$REPO/data"
LOCK_DIR="$LOG_DIR/fedchirp-scan-publish.lockdir"
SUMMARY_FILE="${TMPDIR:-/tmp}/fedchirp-last-scan-summary.md"

mkdir -p "$LOG_DIR"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') another fed-chirp scan/publish run is already active"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

export PATH="/Users/dima/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export FED_CHIRP_HERMES_BIN="${FED_CHIRP_HERMES_BIN:-/Users/dima/.local/bin/hermes}"
export FED_CHIRP_HERMES_PROVIDER="${FED_CHIRP_HERMES_PROVIDER:-openai-codex}"
export FED_CHIRP_HERMES_MODEL="${FED_CHIRP_HERMES_MODEL:-gpt-5.5}"
export FED_CHIRP_HERMES_TIMEOUT="${FED_CHIRP_HERMES_TIMEOUT:-300}"

cd "$REPO"

echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') fed-chirp scan/publish starting"

git fetch origin main

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Working tree has pre-existing changes; refusing to run automated publisher."
  git status --short
  exit 1
fi

git pull --ff-only origin main

rm -f "$SUMMARY_FILE"
"$REPO/.venv/bin/python" -m fed_chirp.cli scan --summary-file "$SUMMARY_FILE"

git add data/fed_chirp.sqlite dashboard/index.html

if git diff --cached --quiet; then
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') no dashboard/database changes to publish"
  exit 0
fi

git config user.name "fed-chirp-local-bot"
git config user.email "fed-chirp-local-bot@users.noreply.github.com"

if [ -s "$SUMMARY_FILE" ]; then
  git commit -F "$SUMMARY_FILE"
else
  git commit -m "scan: $(date -u +'%Y-%m-%d %H:%M UTC')"
fi

git push origin HEAD:main

echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') fed-chirp scan/publish finished"
