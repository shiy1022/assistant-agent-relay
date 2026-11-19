#!/bin/bash
# Embedded in the signed Shortcuts download; no Terminal window is opened.
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/local/go/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
umask 077
origin='https://restaurant-study-demo.onrender.com'
base="$HOME/Library/Application Support/Study Connector"
mkdir -p "$base"
log="$base/shortcut-setup.log"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Get input before the download, so Run immediately gives visible feedback.
if ! /usr/bin/osascript -e '
with timeout of 86400 seconds
tell application "Shortcuts"
activate
text returned of (display dialog "Copy a pairing code from the Study website and paste it below. Setup will then install the connector and pair this Mac. Keep the Study webpage open to continue with your selected agent. First-time setup may take a few minutes." default answer "" buttons {"Cancel", "Connect"} default button "Connect" cancel button "Cancel" with title "Connect Study")
end tell
end timeout
' > "$work/code" 2> "$work/dialog-error"; then
  if /usr/bin/grep -q -- '(-128)' "$work/dialog-error"; then exit 0; fi
  cat "$work/dialog-error" >&2
  exit 1
fi
/usr/bin/osascript -e 'display notification "Installing Study Connector. Keep Shortcuts open; return to the Study webpage when setup finishes." with title "Study · Setting up this Mac"' || true
if ! curl --fail --silent --show-error --location --proto '=https' --connect-timeout 20 --max-time 90 \
  https://raw.githubusercontent.com/MikeDeng2002/assistant-agent-relay/codex/restaurant-study-imessage/scripts/install-study-connector.sh \
  -o "$work/install.sh" 2> "$log" || ! /bin/bash "$work/install.sh" "$origin" --terminal >> "$log" 2>&1; then
  /usr/bin/osascript - "$log" <<'APPLESCRIPT'
on run argv
  with timeout of 86400 seconds
tell application "Shortcuts"
  activate
  set answer to display dialog "Installation could not finish. Check your internet connection and run Connect Study again. You do not need to open Terminal." buttons {"Close", "View details"} default button "Close" with title "Study · Setup interrupted" with icon caution
  end tell
end timeout
  if button returned of answer is "View details" then
    do shell script "/usr/bin/open -a TextEdit " & quoted form of (item 1 of argv)
  end if
end run
APPLESCRIPT
  exit 0
fi
cd "$base/assistant-agent-relay"
.venv/bin/python - "$work/code" <<'PY'
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from bench.study_connector import ConnectorError, request, credential_file, store, agent_channels, CODE, STATE
from bench.study_devices import digest
from bench.config import LOCAL_AGENTS_FILE

ORIGIN = 'https://restaurant-study-demo.onrender.com'

def dialog(message, *, entry=False):
    script = '''on run argv
      with timeout of 86400 seconds
tell application "Shortcuts"
      activate
      text returned of (display dialog (item 1 of argv) default answer "" buttons {"Cancel", "Connect"} default button "Connect" cancel button "Cancel" with title "Connect Study")
      end tell
end timeout
    end run''' if entry else '''on run argv
      with timeout of 86400 seconds
tell application "Shortcuts"
      activate
      display dialog (item 1 of argv) buttons {"OK"} default button "OK" with title "Study Connector"
      end tell
end timeout
    end run'''
    result = subprocess.run(['osascript', '-e', script, message], capture_output=True, text=True)
    if result.returncode:
        # Cancel is intentional; other errors must not appear as successful pairing.
        if '-128' in result.stderr:
            raise SystemExit(0)
        raise RuntimeError('Could not display the setup window. Open Shortcuts and run Connect Study again.')
    return result.stdout.strip()

try:
    code = Path(sys.argv[1]).read_text().strip()
    while True:
        if not CODE.fullmatch(code):
            code = dialog('That is not a complete pairing code. In Study, click Generate pairing code, then copy and paste all 24 characters here.', entry=True)
            continue
        try:
            current = request(ORIGIN, 'pair', {'code': code, 'agents': agent_channels()})
            break
        except ConnectorError as error:
            if error.status in (400, 403):
                code = dialog('This code expired or has already been used. Generate a new pairing code on the Study website and paste it here. Installation is complete; it will not run again.', entry=True)
            elif not error.status:
                code = dialog('The Study website could not be reached. Check your internet connection, then paste a fresh pairing code to retry.', entry=True)
            else:
                raise
    path = credential_file(ORIGIN)
    store(path, current)
    label = 'org.agentrelay.study-connector.' + digest(ORIGIN)[:12]
    launchdir = Path.home() / 'Library/LaunchAgents'
    launchdir.mkdir(parents=True, exist_ok=True)
    launchfile = launchdir / (label + '.plist')
    config = {
        'Label': label,
        'ProgramArguments': [sys.executable, '-m', 'bench.study_connector', '--origin', ORIGIN, '--if-paired'],
        'WorkingDirectory': str(Path.cwd()), 'RunAtLoad': True, 'StartInterval': 30,
        'StandardOutPath': str(STATE / 'connector.log'), 'StandardErrorPath': str(STATE / 'connector.log'),
        'EnvironmentVariables': {'PATH': os.environ['PATH']},
    }
    with launchfile.open('wb') as stream:
        plistlib.dump(config, stream)
    launchfile.chmod(0o600)
    domain = f'gui/{os.getuid()}'
    subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
    loaded = subprocess.run(['launchctl', 'bootstrap', domain, str(launchfile)], capture_output=True, text=True)
    if loaded.returncode:
        raise RuntimeError('Pairing succeeded, but macOS could not start the background connector. Run this shortcut again. If macOS shows a background-item notice, allow Study Connector.')

    subprocess.run(['osascript', '-e', 'display notification "This Mac is paired. Return to Study for your selected agent’s login or setup." with title "Study Connector"'], check=False)
    print('This Mac is paired. Return to Study and click Open channel setup if it has not opened automatically.')
except Exception as error:
    # No tokens or pairing codes are included in errors or logs.
    dialog('Setup needs attention: ' + str(error)[:400] + '\n\nRun Connect Study again to retry. No Terminal commands are needed.')
PY
