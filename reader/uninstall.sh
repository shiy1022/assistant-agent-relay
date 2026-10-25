#!/usr/bin/env bash
# Stops bench-reader and removes the LaunchAgent and the installed binary.
# Captured messages in data/inbox are kept.
set -euo pipefail

label="local.personal-agent-benchmark.bench-reader"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$label.plist"
rm -rf "$HOME/Library/Application Support/bench-reader"
echo "Removed bench-reader. Also delete its entry in System Settings → Privacy & Security → Full Disk Access."
