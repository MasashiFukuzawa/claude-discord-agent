# Repository instructions

- Keep all public documentation and comments free of organization-specific names, addresses, paths, IDs, and operational examples.
- Preserve the existing CLI, SQLite schema, dispatch state machine, and clone-based `DISCORD_AGENT_HOME` workflow unless a migration is explicitly designed and tested.
- Claude Code is the only supported agent host.
- Secrets never enter Worker prompts, command arguments, logs, fallback previews, fixtures, or tracked files.
- XDG-owned directories use mode `0700`; credential and mutable state files use `0600` on POSIX.
- Automatic tmux wake remains experimental, opt-in, positive-allowlist-only, and fail-closed.
- Run the checks in [Delegated scope](#delegated-scope) on the final revision before reporting completion.

## Delegated scope

What an agent session may decide and do without asking. It applies to every session in this repository, and goal-running workflows such as an `autopilot` skill read it. It widens nothing in the rules above, [CONTRIBUTING.md](CONTRIBUTING.md), or [SECURITY.md](SECURITY.md): where they differ, those rules win.

### May do without asking

- On a feature branch: edit code, tests, and documentation, run the checks, and open pull requests.
- Choose implementation details where the choice can be undone.
- Remove worktrees, branches, containers, and temporary files created for the current goal, except anything with uncommitted or unpushed work or with data.

### Must ask

- Operations on a live deployment: a running daemon or Controller session, its credentials, its state database, or the Discord channel it serves.
- Deleting data, and any operation that cannot be undone.
- Publishing or sending anything outside this repository other than the pull request itself, such as a release, a package upload, or a Discord message.
- Anything that adds cost.
- Facts only a person knows.

### Checks and merge

- Checks: the commands under "Before opening a pull request" in [CONTRIBUTING.md](CONTRIBUTING.md), which are the CI gate. They must pass on the final revision before completion is reported; this also holds for a one-off change and for a change that is already committed on the branch. CI runs the same commands on every push, so a passing CI run on that revision counts.
- Changes that need a deeper look are not complete until a second model has reviewed them (a `second-opinion` skill where installed; the session chooses the provider): a change that alters which component (Controller, Supervisor, or Worker) can read the Discord bot token, or that relaxes or removes the `0700`/`0600` permission enforcement for credential or state files described in [SECURITY.md](SECURITY.md). A change that only touches nearby code is not one of them.
- Every other change needs the checks only.
- Always review: that the Discord token stays out of Worker reach and the Controller/Supervisor/Worker boundaries in [SECURITY.md](SECURITY.md) are preserved; the credential and state file permission checks (`0700`/`0600`) and the rejection of unsafe files; and the handling of untrusted input (Worker output, tmux pane state, and the redaction of the opt-in result preview).
- Merge: a human approves merges.
