#!/usr/bin/env bash
# Opens a Print Lab page full-screen in Chromium.
#   kiosk.sh panel [port]   -> touch control panel (default)
#   kiosk.sh board [port]   -> TV queue board
PAGE="${1:-panel}"
PORT="${2:-80}"
URL="http://localhost:$PORT/$PAGE"

# Wait for the server (it starts in parallel with the desktop).
for _ in $(seq 1 60); do
  curl -fs -o /dev/null "http://localhost:$PORT/api/state" && break
  sleep 1
done

BROWSER="$(command -v chromium-browser || command -v chromium)"
exec "$BROWSER" --kiosk --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-features=Translate \
  --check-for-update-interval=31536000 --overscroll-history-navigation=0 \
  --user-data-dir="$HOME/.cache/printlab-$PAGE" "$URL"
