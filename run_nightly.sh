#!/usr/bin/env bash
# run_nightly.sh — entrypoint for the nightly EDH deck engine (PRD §8 step 1;
# §6 risk "gishath-local-v2 not running when the job starts").
#
# Usage:
#   caffeinate ./run_nightly.sh
#
# `caffeinate` (no args) holds the Mac awake for the lifetime of this script
# and releases automatically when it exits — see PRD §7 (manual trigger,
# no launchd).
#
# Loads deck_engine/.env (gitignored — copy deck_engine/.env.example) for
# email credentials, checks whether gishath-local-v2 is already running and
# starts it in the background if not, then runs the pipeline. Leaves the
# Flask app running afterward for reuse rather than tearing it down each
# time — matches how Trevor already runs gishath-local-v2 day to day.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# This script runs as a non-interactive bash shell, which never sources
# ~/.zshrc — so PATH additions the Claude Code installer makes there (e.g.
# `~/.local/bin`, its default install location) are invisible here even
# though they work fine in an interactive terminal. Prepend the common
# install locations explicitly rather than relying on any shell rc file.
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

if [ -f "deck_engine/.env" ]; then
  set -a
  # shellcheck source=deck_engine/.env.example
  source "deck_engine/.env"
  set +a
fi

PORT="${PORT:-5003}"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"

if ! curl -fsS -m 3 "$HEALTH_URL" > /dev/null 2>&1; then
  echo "gishath-local-v2 not running on port ${PORT} — starting it in the background..."
  # shellcheck disable=SC1091
  source venv/bin/activate
  nohup python app.py > server.log 2>&1 &
  disown
  # Give it a moment before deck_engine's own wait_for_gishath_app() starts polling.
  sleep 2
else
  echo "gishath-local-v2 already running on port ${PORT}."
fi

# shellcheck disable=SC1091
source venv/bin/activate
python -m deck_engine.run
exit_code=$?

echo "deck_engine run finished with exit code ${exit_code}."
exit "$exit_code"
