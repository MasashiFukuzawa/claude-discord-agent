# Contributing

Changes should preserve the separation between Controller, Worker, and Supervisor, and must not add
credentials, personal paths, organization names, private repository names, or production identifiers.

Before opening a pull request, run:

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

This matches the CI gate exactly. See [docs/architecture.md](docs/architecture.md) for the
component contracts a change must preserve.

Security-sensitive changes should include tests for failure behavior. Do not commit `config/repos.json`,
state databases, `.env` files, Discord channel IDs, or bot tokens.

Before extracting code from a non-public source, maintainers should also run the content checker with an
untracked, private denylist containing organization-specific terms (one fixed string per line):

```bash
PUBLIC_DENYLIST_FILE=/path/to/private-denylist.txt ./scripts/check-public-content.sh
```
