#!/usr/bin/env bash
# Builds bench-whatsapp, installs it outside ~/Documents and writes its
# LaunchAgent. It starts once you link it: uv run bench whatsapp-link
# (run through `uv run bench whatsapp-install`, which also writes agents.json).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/Library/Application Support/agent-runner/whatsapp"
label="local.personal-agent-benchmark.bench-whatsapp"
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$HOME/Library/Logs/bench-whatsapp.log"
domain="gui/$(id -u)"

mkdir -p "$dest" "$(dirname "$plist")" "$(dirname "$log")"
chmod 700 "$dest"
(cd "$here" && go build -o "$dest/bench-whatsapp.new" .)
mv "$dest/bench-whatsapp.new" "$dest/bench-whatsapp"

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$dest/bench-whatsapp</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <!-- Restart after a crash, but not after a clean exit (not linked, or logged out). -->
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>$log</string>
  <key>StandardErrorPath</key>
  <string>$log</string>
</dict>
</plist>
EOF
plutil -lint -s "$plist"

# A bridge that's already running picks up the new build.
if launchctl print "$domain/$label" >/dev/null 2>&1; then
  launchctl kickstart -k "$domain/$label"
fi
