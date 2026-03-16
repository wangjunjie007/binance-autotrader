#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="$(cd "$ROOT/../.." && pwd)"
cd "$ROOT"

LOG_FILE="$WORKSPACE/logs/binance-position-watch.log"
LOCK_DIR="$WORKSPACE/cache/.binance_position_watch.lock"
PID_FILE="$WORKSPACE/cache/binance-position-watch.pid"
mkdir -p "$WORKSPACE/logs" "$WORKSPACE/cache"

if mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$$" > "$LOCK_DIR/pid"
else
  if [[ -f "$LOCK_DIR/pid" ]]; then
    old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      exit 0
    fi
  fi
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  mkdir "$LOCK_DIR"
  echo "$$" > "$LOCK_DIR/pid"
fi
trap 'rm -rf "$LOCK_DIR" >/dev/null 2>&1 || true' EXIT

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
fi

echo "$$" > "$PID_FILE"
POLL_INTERVAL="${BINANCE_BOT_POLL_INTERVAL_SEC:-3}"
exec env BINANCE_BOT_MODE=positions BINANCE_BOT_POLL_INTERVAL_SEC="$POLL_INTERVAL" BINANCE_BOT_LOG_FILE="$LOG_FILE" python3 "$ROOT/binance_autotrader.py" >> "$LOG_FILE" 2>&1
