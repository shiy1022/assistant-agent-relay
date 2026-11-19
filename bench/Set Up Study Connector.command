#!/bin/bash
set -euo pipefail

origin='https://restaurant-study-demo.onrender.com'
installer_url='https://raw.githubusercontent.com/MikeDeng2002/assistant-agent-relay/codex/restaurant-study-imessage/scripts/install-study-connector.sh'
checkout="$HOME/Library/Application Support/Study Connector/assistant-agent-relay"

installer=$(mktemp -t study-connector)
trap 'rm -f "$installer"' EXIT

if ! curl --fail --silent --show-error --location --proto '=https' "$installer_url" -o "$installer" ||
   ! /bin/bash "$installer" "$origin" --terminal; then
  printf '\nInstallation did not finish. Review the error above.\n' >&2
  read -r -p 'Press Return to close. ' || true
  exit 1
fi

printf '\nIn Study, open Connect agent and click Generate pairing code.\n'
printf 'Keep this Terminal window open while using Study.\n\n'
cd "$checkout"
if ! .venv/bin/python -m bench.study_connector --origin "$origin" --pair; then
  printf '\nThe connector stopped. Review the error above before trying again.\n' >&2
  read -r -p 'Press Return to close. ' || true
  exit 1
fi
