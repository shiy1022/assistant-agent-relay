#!/usr/bin/env bash
# Stops bench-whatsapp and removes its LaunchAgent and binary. To also log the
# device out of WhatsApp, run `uv run bench whatsapp-unlink` first. Recorded
# messages in the inbox are kept.
set -euo pipefail

label="local.personal-agent-benchmark.bench-whatsapp"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$label.plist"
rm -f "$HOME/Library/Application Support/agent-runner/whatsapp/bench-whatsapp"
echo "Removed bench-whatsapp. If it's still listed on your phone under WhatsApp → Linked Devices, log it out there."
