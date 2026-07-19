#!/bin/bash
# Double-click launcher for The Foundry — browser version.
# Starts the local server (if it isn't already running) and opens it in
# Chrome. The server is detached (nohup) so it keeps running even if you
# close this Terminal window or the browser tab — an in-progress commission
# survives either one closing.

PROJECT_DIR="/Users/trevorjow/Desktop/Cowork Playground/Local Gishath Fetch/gishath-local-lmstudio"
# 5078 is the LOCAL fork (The Foundry) — 5077 is the original cloud Atelier.
# Exported so atelier/server.py binds the same port we health-check here.
PORT="${ATELIER_PORT:-5078}"
export ATELIER_PORT="$PORT"
URL="http://127.0.0.1:$PORT"
LOG_FILE="$PROJECT_DIR/logs/atelier_server.log"
PID_FILE="$PROJECT_DIR/logs/atelier_server.pid"

cd "$PROJECT_DIR" || {
  echo "Could not find the gishath-local-lmstudio project folder."
  read -p "Press Return to close this window..."
  exit 1
}

is_up() {
  curl -sf -o /dev/null -m 1 "$URL/api/health"
}

if is_up; then
  echo "The Foundry is already running at $URL"
else
  echo "Starting The Foundry server..."
  mkdir -p logs
  source venv/bin/activate
  nohup python -m atelier.server > "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  disown

  for i in $(seq 1 30); do
    is_up && break
    sleep 0.5
  done

  if ! is_up; then
    echo ""
    echo "⚠️  The server didn't come up — check $LOG_FILE for details."
    read -p "Press Return to close this window..."
    exit 1
  fi
  echo "Server is up."
fi

echo "Opening in Chrome..."
open -a "Google Chrome" "$URL" 2>/dev/null || open "$URL"

echo ""
echo "You can close this window now — the server keeps running in the background."
echo "Log: $LOG_FILE"
echo "To stop it later: kill \$(cat \"$PID_FILE\")"
sleep 2
