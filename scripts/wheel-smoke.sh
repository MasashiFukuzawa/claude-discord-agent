#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

wheel=$(find dist -maxdepth 1 -name 'discord_agent-1.0.0-*.whl' -print -quit)
[[ -n "$wheel" ]] || { echo "Build the 1.0.0 wheel first: uv build" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
uv venv "$tmp/venv" >/dev/null
uv pip install --python "$tmp/venv/bin/python" "$wheel" >/dev/null
export XDG_STATE_HOME="$tmp/state"
export XDG_CONFIG_HOME="$tmp/config"
"$tmp/venv/bin/orchestrator" list-specs | grep -q 'example-task'
"$tmp/venv/bin/python" -c 'from lib.runner import _load_system_prompt; assert _load_system_prompt()'
git init "$tmp/example-repo" >/dev/null
"$tmp/venv/bin/orchestrator" create-repo example-repo --path "$tmp/example-repo" >/dev/null
"$tmp/venv/bin/orchestrator" status --json >/dev/null
test "$(stat -f '%Lp' "$tmp/state/claude-discord-agent" 2>/dev/null || stat -c '%a' "$tmp/state/claude-discord-agent")" = 700
test "$(stat -f '%Lp' "$tmp/config/claude-discord-agent" 2>/dev/null || stat -c '%a' "$tmp/config/claude-discord-agent")" = 700
test "$(stat -f '%Lp' "$tmp/config/claude-discord-agent/repos.json" 2>/dev/null || stat -c '%a' "$tmp/config/claude-discord-agent/repos.json")" = 600
echo "wheel smoke: PASS"
