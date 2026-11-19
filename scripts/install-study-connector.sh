#!/bin/bash
# Bootstrap a private Study checkout on a participant's Mac.
set -euo pipefail
origin="${1:-}"
mode="${2:-}"
if [[ -n "$mode" && "$mode" != --terminal ]]; then
  echo "Unknown installation option: $mode" >&2
  exit 1
fi
if [[ "$(uname -s)" != Darwin ]]; then
  echo 'The Study Connector currently requires a Mac.' >&2
  exit 1
fi
if [[ ! "$origin" =~ ^https://[a-zA-Z0-9.-]+(:[0-9]+)?$ ]]; then
  echo 'Use the installation command from your Study website.' >&2
  exit 1
fi
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
base="$HOME/Library/Application Support/Study Connector"
mkdir -p "$base"
chmod 700 "$base"
work="$(mktemp -d "$base/download.XXXXXX")"
trap 'rm -rf "$work"' EXIT
if ! command -v uv >/dev/null 2>&1; then
  echo 'Installing the Python package manager from Astral…'
  curl --fail --location --proto '=https' --tlsv1.2 https://astral.sh/uv/install.sh -o "$work/install-uv.sh"
  sh "$work/install-uv.sh"
fi
checkout="$base/assistant-agent-relay"
echo 'Downloading the latest Study Connector…'
curl --fail --location --proto '=https' --tlsv1.2 \
  https://github.com/MikeDeng2002/assistant-agent-relay/archive/refs/heads/codex/restaurant-study-imessage.tar.gz \
  -o "$work/source.tar.gz"
mkdir "$work/source"
tar -xzf "$work/source.tar.gz" -C "$work/source" --strip-components=1
if [[ ! -d "$checkout" ]]; then
  mv "$work/source" "$checkout"
else
  # Refresh program files, preserving local addresses, credentials, and results.
  for item in bench reader whatsapp scripts pyproject.toml uv.lock; do
    if [[ -d "$work/source/$item" ]]; then
      mkdir -p "$checkout/$item"
      cp -R "$work/source/$item/." "$checkout/$item/"
    else
      cp "$work/source/$item" "$checkout/$item"
    fi
  done
fi
cd "$checkout"
uv sync --python 3.12 --locked --no-dev
if [[ "$mode" == --terminal ]]; then
  printf '\nReturn to %s/study and generate a pairing code.\n' "$origin"
else
  uv run --no-sync python -m bench.study_connector --origin "$origin" --install
  printf '\nReturn to %s/study to pair this Mac.\n' "$origin"
fi
