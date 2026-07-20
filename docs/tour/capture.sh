#!/usr/bin/env bash
# Regenerate the visual tour (docs/tour/*.png + failover.gif) from a running
# console. Reproducible: point it at any k8ost-console + CNPG cluster and re-run.
#
#   TOUR_CONTEXT=kind-k8ost TOUR_NS=demo TOUR_CLUSTER=orders bash docs/tour/capture.sh
#
# Prereqs: the console running at $TOUR_URL (uv run --directory pg k8ost-console),
# a reachable CNPG cluster, Google Chrome, gifski, node >= 21, kubectl.
set -euo pipefail

export TOUR_URL="${TOUR_URL:-http://127.0.0.1:8700}"
export TOUR_NS="${TOUR_NS:-demo}"
export TOUR_CLUSTER="${TOUR_CLUSTER:-orders}"
export TOUR_CONTEXT="${TOUR_CONTEXT:-kind-k8ost}"
export TOUR_OUT="${TOUR_OUT:-docs/tour}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PROF="$(mktemp -d)"

curl -sf -o /dev/null "$TOUR_URL/" || { echo "console not reachable at $TOUR_URL"; exit 1; }

"$CHROME" --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
  --remote-debugging-port=9222 --user-data-dir="$PROF" --remote-allow-origins=* \
  >/dev/null 2>&1 &
CHROME_PID=$!
trap 'kill $CHROME_PID 2>/dev/null || true; rm -rf "$PROF" 2>/dev/null || true' EXIT
for _ in $(seq 1 15); do curl -sf -o /dev/null http://127.0.0.1:9222/json/version && break; sleep 1; done

node "$HERE/capture.mjs"

# frames -> a looping GIF. gifski keeps every frame's timing and quantises well,
# so the transition stays readable and the asset stays light (~150KB).
gifski --fps 5 --width 820 --quality 80 -o "$TOUR_OUT/failover.gif" "$TOUR_OUT"/frames/f*.png >/dev/null 2>&1
[ -n "${KEEP_FRAMES:-}" ] || rm -rf "$TOUR_OUT/frames"
echo "wrote $TOUR_OUT/{01-operate,02-build,03-breakglass}.png and failover.gif"
