#!/usr/bin/env bash
# Lighthouse Trading — Stop server
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_ROOT/lighthouse.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "No PID file found — is Lighthouse Trading running?"
  exit 0
fi

PID=$(cat "$PID_FILE")

if kill -0 "$PID" 2>/dev/null; then
  echo "Stopping Lighthouse Trading (PID $PID)..."
  kill -SIGTERM "$PID"

  # Wait up to 10 seconds for graceful shutdown
  for i in $(seq 1 10); do
    if ! kill -0 "$PID" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  if kill -0 "$PID" 2>/dev/null; then
    echo "Process did not stop gracefully — sending SIGKILL"
    kill -9 "$PID" || true
  fi

  rm -f "$PID_FILE"
  echo "Lighthouse Trading stopped"
else
  echo "Process $PID is not running — cleaning up PID file"
  rm -f "$PID_FILE"
fi
