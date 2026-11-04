#!/usr/bin/env sh
# Stops the upscale service started by install-autostart.sh (or start-service.sh).
set -eu

pids=$(pgrep -f "service/server\.py" 2>/dev/null || true)
if [ -z "$pids" ]; then
  echo "No upscale service process is running."
  exit 0
fi

for pid in $pids; do
  kill "$pid" 2>/dev/null && echo "Stopped pid $pid." || echo "Could not stop pid $pid."
done
