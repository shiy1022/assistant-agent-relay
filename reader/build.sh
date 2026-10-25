#!/usr/bin/env bash
# Builds bench-reader with the agent handles from an agents.yaml compiled in.
#
#   reader/build.sh                 config/agents.yaml → reader/bin/bench-reader
#   reader/build.sh --test          test build: also accepts --db and --attachments-root
#   --config FILE, --out FILE       read another agents.yaml, write somewhere else
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
config="$root/config/agents.yaml"
out="$here/bin/bench-reader"
flags=(-O)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test) flags+=(-D TESTING); shift ;;
    --config) config="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    *) echo "build.sh: unknown argument $1" >&2; exit 64 ;;
  esac
done

gen="$(mktemp -d)"
trap 'rm -rf "$gen"' EXIT

uv run --project "$root" --quiet python -m bench.allowlist "$config" > "$gen/Allowlist.swift"
mkdir -p "$(dirname "$out")"
swiftc "${flags[@]}" -o "$out" "$here/main.swift" "$gen/Allowlist.swift"
# An ad-hoc signature with a fixed identifier. macOS ties the Full Disk Access
# grant to this exact build, so a rebuilt binary has to be approved again.
codesign --force --sign - --identifier local.personal-agent-benchmark.bench-reader "$out"
