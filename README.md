# claude-discord-agent

A local, asynchronous multi-repository job controller that connects Discord to Claude Code.
The Controller accepts work, Workers run `claude -p`, and a non-LLM Supervisor guarantees a
terminal delivery attempt without giving Workers direct access to the Discord token.

> [!IMPORTANT]
> This project is Claude Code-only. It depends on Claude Code CLI behavior and optionally tmux.
> It does not claim compatibility with Codex or other agent hosts.

## Requirements

- Python 3.12 or later on Linux or macOS
- Claude Code CLI (`claude`)
- A Discord bot token and destination channel ID
- tmux only when using the experimental automatic wake feature

Windows is not currently supported. The runtime uses POSIX ownership and permission checks,
signals, and daemon behavior. WSL may work but is not part of the tested support matrix.

## Install

The recommended installation is an isolated executable from GitHub:

```bash
uv tool install git+https://github.com/MasashiFukuzawa/claude-discord-agent.git
orchestrator --help
```

When installing from a local checkout, note that `uv tool install --force .` reuses the cached
wheel while the version number is unchanged — add `--reinstall` to pick up local changes.

Clone-based installs remain supported for operators who want a stable runtime checkout:

```bash
git clone https://github.com/MasashiFukuzawa/claude-discord-agent.git "$HOME/.local/share/claude-discord-agent"
cd "$HOME/.local/share/claude-discord-agent"
uv sync --all-extras --locked
export DISCORD_AGENT_HOME="$HOME/.local/share/claude-discord-agent"
python3 "$DISCORD_AGENT_HOME/orchestrator.py" --help
```

`DISCORD_AGENT_HOME` is a compatibility path for clone-based operation. A `uv tool` install loads
the worker prompt and bundled example specs from package resources and does not require that variable.

## Configure

Inject secrets from the environment or a secret manager:

```bash
export DISCORD_BOT_TOKEN="..."
export DISCORD_NOTIFY_CHAT_ID="<channel-id>"
```

Alternatively create `${XDG_CONFIG_HOME:-$HOME/.config}/claude-discord-agent/env` as an
owner-controlled regular file with mode `0600`. The containing configuration and state directories
must be `0700`; state files are tightened to `0600`. Symlinked, foreign-owned, or group/world-readable
credential files are rejected.

```dotenv
DISCORD_BOT_TOKEN=replace-with-your-token
```

## Quick start

Use `orchestrator` below. Clone-based users may substitute
`python3 "$DISCORD_AGENT_HOME/orchestrator.py"`.

```bash
orchestrator daemon start
orchestrator create-repo example-app --path "$HOME/src/example-app"
orchestrator dispatch example-app "Run the tests and fix the failure" \
  --notify-chat-id "$DISCORD_NOTIFY_CHAT_ID"
orchestrator status
orchestrator collect example-app --json
```

`daemon start` detaches and logs to `daemon.log` in the state directory, including crash
tracebacks. To run the daemon under a process supervisor (launchd, systemd), use
`daemon start --foreground` so keep-alive supervision works; the self-forking mode is for
manual, unsupervised use.

Mutable state lives under `${XDG_STATE_HOME:-$HOME/.local/state}/claude-discord-agent/`.
The SQLite registry is authoritative. Export/import data is written to
`${XDG_CONFIG_HOME:-$HOME/.config}/claude-discord-agent/repos.json`. Existing clone-based
`config/repos.json` files remain a read-only fallback for migration.

## Delivery safety

- Direct fallback notification is the default and does not include Worker result text.
- `daemon start --fallback-result-preview` opts into a redacted, 200-character preview. Redaction is
  defense in depth, not permission to place secrets in Worker output.
- Automatic tmux wake is experimental, disabled by default, and fail-closed. `--auto-wake` sends keys
  only when a positive prompt allowlist identifies an idle Controller; unknown UI states fall back safely.
- Workers never receive the Discord token and are never launched with permission-bypass flags.
- Destructive, production, or privilege-expanding work requires user confirmation before dispatch.

## Claude Code plugin

Add this repository as a Claude Code marketplace and install the `discord-agent` plugin. The skill
uses the installed `orchestrator` executable when available and falls back to `DISCORD_AGENT_HOME`
for clone-based deployments.

## Development

```bash
uv sync --all-extras --locked
uv run ruff check .
uv run ty check lib orchestrator.py
uv run pytest
uv build
./scripts/wheel-smoke.sh
uv run ./scripts/validate-plugin.py
./scripts/check-public-content.sh
```

See [docs/architecture.md](docs/architecture.md) for the full architecture and operational
contracts (an HTML overview is at [docs/architecture.html](docs/architecture.html)), and
[SECURITY.md](SECURITY.md) for the threat model and reporting instructions.

## License

Apache License 2.0. See [LICENSE](LICENSE).
