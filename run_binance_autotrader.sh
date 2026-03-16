#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="$(cd "$ROOT/../.." && pwd)"
cd "$ROOT"

LOG_FILE="$WORKSPACE/logs/binance-autotrader.log"
PID_FILE="$WORKSPACE/cache/binance-autotrader.pid"
LOCK_DIR="$WORKSPACE/cache/.binance_autotrader.lock"

mkdir -p "$WORKSPACE/logs" "$WORKSPACE/cache"

if mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$$" > "$LOCK_DIR/pid"
else
  if [[ -f "$LOCK_DIR/pid" ]]; then
    old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      echo "[INFO] binance autotrader already running pid=${old_pid}" >> "$LOG_FILE"
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

exec python3 "$ROOT/binance_autotrader.py" >> "$LOG_FILE" 2>&1
