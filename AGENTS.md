# Repository instructions

- Keep all public documentation and comments free of organization-specific names, addresses, paths, IDs, and operational examples.
- Preserve the existing CLI, SQLite schema, dispatch state machine, and clone-based `DISCORD_AGENT_HOME` workflow unless a migration is explicitly designed and tested.
- Claude Code is the only supported agent host.
- Secrets never enter Worker prompts, command arguments, logs, fallback previews, fixtures, or tracked files.
- XDG-owned directories use mode `0700`; credential and mutable state files use `0600` on POSIX.
- Automatic tmux wake remains experimental, opt-in, positive-allowlist-only, and fail-closed.
- Run the full commands in `.agents/done.yml` before reporting completion.
