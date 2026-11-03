#!/usr/bin/env sh
# Starts the upscale service in the foreground (console shows live logs).
# macOS / Linux pair of tools/start-service.cmd on Windows.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
python="${PYTHON:-python3}"

command -v "$python" >/dev/null 2>&1 || {
  echo "Python 3.10+ was not found. Set PYTHON=/path/to/python3 and retry." >&2
  exit 1
}

exec "$python" "$root/service/server.py" "$@"
