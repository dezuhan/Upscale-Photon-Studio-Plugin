#!/usr/bin/env sh
# Starts the upscale service at login on macOS (launchd) or Linux (systemd --user).
#
#   ./tools/install-autostart.sh            install + start now
#   ./tools/install-autostart.sh --remove   stop and remove the autostart entry
#
# No administrator rights are needed: a per-user LaunchAgent / user unit is used.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
server="$root/service/server.py"
logs="$root/service/logs"
port="${PORT:-8765}"
engine="${ENGINE:-auto}"
label="studio.tenzen.photon-upscale.service"

[ -f "$server" ] || { echo "server.py not found at $server" >&2; exit 1; }
python=$(command -v "${PYTHON:-python3}" || true)
[ -n "$python" ] || { echo "Python 3.10+ was not found on PATH." >&2; exit 1; }

remove_autostart() {
  if [ "$(uname -s)" = "Darwin" ]; then
    launchctl unload "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$label.plist"
  else
    systemctl --user disable --now photon-upscale.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/photon-upscale.service"
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  pids=$(pgrep -f "service/server\.py" 2>/dev/null || true)
  for pid in $pids; do kill "$pid" 2>/dev/null || true; done
  echo "Autostart removed and the service stopped."
}

case "${1:-}" in
  -r|--remove|remove)
    remove_autostart
    exit 0
    ;;
esac

mkdir -p "$logs"

if [ "$(uname -s)" = "Darwin" ]; then
  agents="$HOME/Library/LaunchAgents"
  mkdir -p "$agents"
  cat > "$agents/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$python</string>
    <string>$server</string>
    <string>--port</string><string>$port</string>
    <string>--engine</string><string>$engine</string>
  </array>
  <key>WorkingDirectory</key><string>$root/service</string>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$logs/service.log</string>
  <key>StandardErrorPath</key><string>$logs/service.log</string>
</dict>
</plist>
PLIST
  launchctl unload "$agents/$label.plist" 2>/dev/null || true
  launchctl load "$agents/$label.plist"
  echo "LaunchAgent installed: $agents/$label.plist"
else
  units="$HOME/.config/systemd/user"
  mkdir -p "$units"
  cat > "$units/photon-upscale.service" <<UNIT
[Unit]
Description=Photon Upscale local service

[Service]
ExecStart="$python" "$server" --port $port --engine $engine
WorkingDirectory=$root/service
Restart=on-failure
StandardOutput=append:$logs/service.log
StandardError=append:$logs/service.log

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now photon-upscale.service
  echo "systemd user unit installed: $units/photon-upscale.service"
fi

# Give it a moment, then report.
i=0
while [ $i -lt 20 ]; do
  sleep 0.5
  if "$python" - "$port" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=2) as r:
    assert json.loads(r.read().decode())["ok"]
PY
  then
    echo "Service is answering on http://127.0.0.1:$port"
    echo "Log: $logs/service.log"
    exit 0
  fi
  i=$((i + 1))
done

echo "The service did not answer yet. Check $logs/service.log" >&2
exit 1
