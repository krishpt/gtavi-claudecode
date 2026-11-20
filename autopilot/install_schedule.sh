#!/bin/bash
# Installs a macOS launchd job that runs the autopilot every 30 minutes.
# Usage: bash autopilot/install_schedule.sh        (uninstall: bash autopilot/install_schedule.sh --remove)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$DIR")"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
LABEL="com.gtavi.autopilot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--remove" ]; then rm -f "$PLIST"; echo "removed"; exit 0; fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>-lc</string>
    <string>cd "$DIR" &amp;&amp; [ -f tiktok.env ] &amp;&amp; set -a &amp;&amp; . ./tiktok.env &amp;&amp; set +a; exec "$PY" autoclip.py once</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$ROOT/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$(dirname "$(command -v claude || echo /usr/local/bin/claude)")</string>
  </dict>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$DIR/launchd.err.log</string>
</dict></plist>
PL
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: runs every 30 min. Logs: $DIR/autoclip.log"
