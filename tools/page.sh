#!/usr/bin/env bash
# Start the quick demo dashboard, check the page in a browser, stop the demo.
#   bash tools/page.sh                 # check only
#   bash tools/page.sh --shots <dir>   # also save a screenshot per tab and width
# The demo listens on port 8765 (BP_DEMO_PORT to change). Takes about 40 s.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${BP_DEMO_PORT:-8765}"
LOG="$(mktemp)"
python tools/demo_dashboard.py --quick --port "$PORT" >"$LOG" 2>&1 &
DEMO=$!
trap 'kill "$DEMO" 2>/dev/null || true; rm -f "$LOG"' EXIT
for _ in $(seq 120); do
  grep -q "Serving on" "$LOG" && break
  kill -0 "$DEMO" 2>/dev/null || { echo "The demo dashboard stopped:"; cat "$LOG"; exit 1; }
  sleep 1
done
grep -q "Serving on" "$LOG" || { echo "The demo dashboard did not start in 120 s:"; cat "$LOG"; exit 1; }
node tools/pagecheck.cjs "http://127.0.0.1:$PORT/" "$@"
