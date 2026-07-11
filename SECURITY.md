# Security policy

## Supported versions

Security fixes are applied to the latest `1.x` release.

## Reporting a vulnerability

Use GitHub private vulnerability reporting for this repository. Do not open a public issue containing
tokens, channel IDs, repository paths, Worker output, or reproduction data from a real environment.

## Trust boundaries

- The Controller and Supervisor may access Discord delivery configuration.
- Workers receive a task and repository working directory, but not the Discord bot token.
- Registered repositories and Worker output are untrusted input.
- tmux pane content is untrusted UI state. Automatic wake acts only on a positive idle-state allowlist.
- Fallback result previews are disabled by default. The opt-in preview is length-limited and redacted.

## Local permissions

On POSIX systems, XDG configuration/state directories must be owner-only (`0700`) and mutable state or
credential files must be owner-readable/writable only (`0600`). The runtime rejects unsafe credential
files and tightens state files it creates. Operators remain responsible for backups, shell history,
process inspection, and the permissions of registered repositories.
