#!/usr/bin/env bash
# Lighthouse Trading — Start server
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Load .env if present
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

HOST="${LIGHTHOUSE_HOST:-0.0.0.0}"
PORT="${LIGHTHOUSE_PORT:-8420}"
LOG_LEVEL="${LOG_LEVEL:-info}"
PID_FILE="$PROJECT_ROOT/lighthouse.pid"

# Check if already running
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "Lighthouse Trading is already running (PID $PID)"
    exit 0
  else
    rm -f "$PID_FILE"
  fi
fi

echo "Starting Lighthouse Trading on $HOST:$PORT..."
uvicorn main:app \
  --host "$HOST" \
  --port "$PORT" \
  --log-level "$LOG_LEVEL" \
  --no-access-log \
  &

echo $! > "$PID_FILE"
echo "Started (PID $(cat "$PID_FILE"))"
echo "Logs: tail -f lighthouse.log"
echo "Health: curl http://localhost:$PORT/health"
