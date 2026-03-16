#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

pkill -f "binance_autotrader.py" >/dev/null 2>&1 || true
sleep 1

nohup "$ROOT/run_binance_signals.sh" >/dev/null 2>&1 &
nohup "$ROOT/run_binance_trade.sh" >/dev/null 2>&1 &
nohup "$ROOT/run_binance_position_watch.sh" >/dev/null 2>&1 &

sleep 2
pgrep -fl "binance_autotrader.py" | head -n 20

echo "--- logs ---"
WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"
ls -1 "$WORKSPACE/logs" | grep -E '^binance-(signals|trade|position-watch)\.log$' || true
