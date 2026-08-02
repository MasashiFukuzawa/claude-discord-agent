# Architecture and operational specification

Audience: AI agents (and maintainers) working on or operating this codebase. This file states
the current contracts precisely; `docs/architecture.html` is the human-oriented overview of the
same material. When behavior and this document disagree, treat the code as authoritative and fix
the document in the same change.

## System roles

Three roles with strict trust boundaries (see `SECURITY.md`):

| Role | Process | May touch Discord token | Purpose |
| --- | --- | --- | --- |
| Controller | Interactive Claude Code session (usually in tmux) running the `discord-agent` skill | via its own channel plugin only | Accepts requests from Discord, dispatches jobs, replies with collected results |
| Orchestrator daemon | `orchestrator daemon start` (single process: Runner + Watchdog + Scheduler + Supervisor) | yes (Supervisor fallback) | Runs the job queue, monitors Workers, guarantees a terminal delivery attempt |
| Worker | `claude -p` spawned per job in the registered repository | **never** | Executes one task, prints a structured result block |

The Controller never edits code itself; it delegates to Workers. Workers never receive the
Discord token and are never launched with permission-bypass flags.

## Job lifecycle

States (`lib/db.py JOB_STATES`):

```
queued -> starting -> running -> succeeded
                              -> failed_retryable -> (requeued by Scheduler)
                              -> failed_permanent
                              -> rate_limited     -> (requeued after backoff)
                              -> needs_controller
                              -> timed_out | lost -> (Watchdog resolves)
```

Delivery-terminal states (`DELIVERY_TERMINAL_STATES`) are `succeeded`, `failed_permanent`,
`needs_controller`. Only these trigger Supervisor delivery; `timed_out`/`lost` are intermediate
and are resolved by the Watchdog first. All transitions are validated against
`VALID_TRANSITIONS`; invalid transitions raise.

A job carries: `task` (with an idempotency preamble automatically prepended at dispatch),
`notify_chat_id`, optional `next_dispatch_payload` (chain), `chain_depth` (recursion guard),
`enqueue_at` (scheduling), and the delivery bookkeeping below.

## Delivery guarantee (Supervisor)

Goal: every delivery-terminal job produces exactly one notification outcome, even if the
Controller session is dead.

Bookkeeping columns: `terminal_at` (stable timestamp of the terminal transition), `wake_state`
(`NULL` → `woken` → `fallback` → `undeliverable`), `wake_attempts`, `reported_at` (set by
`report-done`; acts as CAS idempotency marker).

Per tick, for each undelivered terminal job:

1. **Wake path (experimental, opt-in `--auto-wake`)**: if a Controller pane is registered
   (`register-pane`) and tmux reports it idle against a positive prompt allowlist, send a
   single-line wake instruction via `tmux send-keys`. Unknown pane state = do not send
   (fail-closed). The woken Controller runs `collect --json`, replies on Discord, then calls
   `report-done <job-id>`, which suppresses further delivery.
2. **Fallback path (default)**: claim the job via compare-and-swap (`claim_for_fallback`,
   exactly-once across ticks), then POST directly to the Discord API. The default message
   contains job id and state but **no Worker output**. `daemon start --fallback-result-preview`
   opts into a 200-character redacted preview; redaction is defense in depth, not permission to
   put secrets in Worker output.
3. Transient failures (5xx / network / 429) retry next tick; permanent failures (4xx / missing
   token) move to `undeliverable`.

`report-done` must be called only after a successful Discord reply. It is idempotent.

## Daemon lifecycle

`orchestrator daemon start` double-forks: the parent prints the PID and exits; the child calls
`setsid()`, then **replaces file descriptors 0/1/2 via `dup2`** (stdin → `/dev/null`,
stdout/stderr → `daemon.log`, created `0600`). Rebinding `sys.stdout` alone is not enough: the
inherited descriptors would keep any pipe held open, blocking every caller that reads the
command's output (wrappers, shells, CI) for the daemon's whole lifetime. Regression tests:
`tests/test_daemon_detach.py`.

Logging: a root handler (name `discord-agent-daemon`) is attached in both modes, INFO level,
writing to stderr — which is `daemon.log` when detached and the supervisor's captured stream in
foreground mode. Without it, `logger.exception()` on the crash path writes nowhere and an
unattended failure leaves no trail.

`daemon start --foreground` is the mode a process supervisor (launchd, systemd) should run:
the process stays in the foreground so `KeepAlive`-style supervision works. The forking mode
defeats supervision because the supervised parent exits immediately.

Duplicate starts are refused via `daemon.pid` (stale PID files are detected). `daemon stop`
sends SIGTERM; the daemon stops Workers, removes the PID file, and exits.

## Storage and file layout

SQLite (`state.db`, WAL) is the single source of truth.

| Path | Contents | Mode |
| --- | --- | --- |
| `${XDG_STATE_HOME:-~/.local/state}/claude-discord-agent/` | `state.db`, `daemon.pid`, `daemon.log`, `controller.pane` | dir `0700`, files `0600` |
| `${XDG_CONFIG_HOME:-~/.config}/claude-discord-agent/` | `env` (credentials), `repos.json` (export) | dir `0700`, files `0600` |

Credential file reading is hardened (`lib/notifier.py`): symlinks, non-regular files,
foreign-owned files, and group/world-readable files are rejected. `DISCORD_AGENT_ENV_FILE`
overrides the credential file location; environment variables take precedence over the file.
`--db` relocates the database, and the PID/log files follow it.

## Deployment contract (what a working installation requires)

1. **Runtime**: `uv tool install git+<repo-url>` (isolated executable) or a clone with
   `DISCORD_AGENT_HOME` set. Note for source installs: `uv tool install --force .` reuses the
   cached wheel when the version number is unchanged — pass `--reinstall` to pick up local
   changes.
2. **Daemon under supervision**: run `daemon start --foreground` from a process supervisor with
   keep-alive, or `daemon start` manually (unsupervised).
3. **Controller session**: an interactive Claude Code session started with the Discord channel
   plugin attached, kept alive (tmux + a restart loop is the reference shape). The
   `discord-agent` skill defines its behavior.
4. **Exactly one gateway connection per bot token.** The Discord channel plugin logs in
   unconditionally per MCP-server instance. A second login on the same token causes the later
   MCP server to exit at startup, which the host reports as a failed plugin — the session then
   looks healthy but receives nothing. Any watchdog for the Controller must check that the
   session's Discord MCP child process exists (allowing a startup grace period), not merely that
   the session is alive, and must treat competing gateway connections (other agent sessions,
   IDE-embedded MCP clients, orphaned server processes) as faults to remove.
5. **Environment**: the daemon needs `DISCORD_BOT_TOKEN` (env or credential file) and jobs need
   `--notify-chat-id` or `DISCORD_NOTIFY_CHAT_ID`. Never place these in tracked files.

## Verification

`.agents/done.yml` intentionally keeps `verify` empty because CI runs the full gate on every
push. The gate (also in `CONTRIBUTING.md` / `README.md`):

```
uv run ruff check .
uv run ty check lib orchestrator.py
uv run pytest
uv build
./scripts/wheel-smoke.sh
uv run ./scripts/validate-plugin.py
./scripts/check-public-content.sh
```

`check-public-content.sh` rejects personal paths and email addresses in tracked files; keep all
operator-specific values (channel IDs, hostnames, absolute paths) out of the repository —
deployment-specific runbooks belong outside the tree.
