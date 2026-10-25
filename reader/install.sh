#!/usr/bin/env bash
# Builds bench-reader from config/agents.yaml, installs it outside ~/Documents
# and (re)starts it as a LaunchAgent, so launchd (not Terminal or Claude) is
# what launches it and macOS checks bench-reader's own Full Disk Access.
#
# After every install, approve the binary in System Settings → Privacy &
# Security → Full Disk Access: a rebuilt binary is a new build.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
label="local.personal-agent-benchmark.bench-reader"
dest="$HOME/Library/Application Support/bench-reader"
bin="$dest/bench-reader"
plist="$HOME/Library/LaunchAgents/$label.plist"
inbox="$root/data/inbox"
log="$HOME/Library/Logs/bench-reader.log"
domain="gui/$(id -u)"

"$here/build.sh"

launchctl bootout "$domain/$label" 2>/dev/null || true
mkdir -p "$dest" "$inbox" "$(dirname "$plist")" "$(dirname "$log")"
cp "$here/bin/bench-reader" "$bin"

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$bin</string>
    <string>--out</string>
    <string>$inbox</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>$log</string>
  <key>StandardErrorPath</key>
  <string>$log</string>
</dict>
</plist>
EOF
plutil -lint -s "$plist"
launchctl bootstrap "$domain" "$plist"

cat <<EOF

bench-reader is installed and running, but it can't read Messages until you approve it:

  1. In System Settings → Privacy & Security → Full Disk Access (opening now), click +.
  2. Press ⌘⇧G, paste this path and click Open:
       $bin
  3. Restart it:
       launchctl kickstart -k $domain/$label
  4. Check it:
       uv run bench reader-status

EOF
open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" || true
