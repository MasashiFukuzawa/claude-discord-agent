#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

patterns=(
  '/Users/[^/]+'
  '/home/[^/]+/(work|src|projects)/[^/]+'
  '[[:alnum:]._%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,}'
)

files=()
while IFS= read -r -d '' file; do
  files+=("$file")
done < <(git ls-files -co --exclude-standard -z | grep -zv '^scripts/check-public-content\.sh$')

for pattern in "${patterns[@]}"; do
  if ((${#files[@]})) && rg -n -i --pcre2 "$pattern" "${files[@]}"; then
    echo "Public-content check failed for a prohibited pattern." >&2
    exit 1
  fi
done

# Organization-specific terms are supplied by a private, untracked audit input.
# Each non-empty line is treated as a fixed string.
if [[ -n "${PUBLIC_DENYLIST_FILE:-}" ]]; then
  while IFS= read -r term; do
    [[ -z "$term" || "$term" == \#* ]] && continue
    if ((${#files[@]})) && rg -n -i --fixed-strings "$term" "${files[@]}"; then
      echo "Private denylist check failed." >&2
      exit 1
    fi
  done < "$PUBLIC_DENYLIST_FILE"
fi
