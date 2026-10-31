#!/bin/bash
# Synchronous: the scheduler must receive the real publisher exit code.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${FED_CHIRP_REPO:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export FED_CHIRP_REPO="$REPO"
export FED_CHIRP_PYTHON="${FED_CHIRP_PYTHON:-$REPO/.venv/bin/python}"
export FED_CHIRP_HERMES_BIN="${FED_CHIRP_HERMES_BIN:-$HOME/.local/bin/hermes}"
export FED_CHIRP_HERMES_PROVIDER="${FED_CHIRP_HERMES_PROVIDER:-openai-codex}"
export FED_CHIRP_HERMES_MODEL="${FED_CHIRP_HERMES_MODEL:-gpt-6-astra}"
export FED_CHIRP_HERMES_TIMEOUT="${FED_CHIRP_HERMES_TIMEOUT:-300}"
# The Hermes client uses the supported `tool` source (not a custom source).
exec "$FED_CHIRP_PYTHON" "$SCRIPT_DIR/publish.py" "$@"
