#!/usr/bin/env bash
# Usage: agent-rules-sync.sh <repo-url> <ref> <dest>
# Clone/fetch the shared agent-rules repo into <dest>, check out <ref> (detached; fails if the ref does not
# exist), run its install.sh, and print a final `rules=<short sha> changed=<true|false>` line.
# Idempotent: install.sh runs on every invocation (it is itself idempotent); `changed` tracks HEAD only.
set -euo pipefail

url=${1:?repo url}; ref=${2:?ref}; dest=${3:?dest}
if [[ -z $ref || $ref == -* || $ref == *..* || ! $ref =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
  echo "agent-rules: unsafe ref" >&2; exit 2
fi
if [[ $url == -* ]]; then echo "agent-rules: unsafe repo url" >&2; exit 2; fi
export GIT_TERMINAL_PROMPT=0

before=none
if [[ -d $dest/.git ]]; then
  before=$(git -C "$dest" rev-parse HEAD 2>/dev/null || echo none)
  git -C "$dest" remote set-url origin "$url"
else
  git clone --quiet -- "$url" "$dest"
fi
git -C "$dest" fetch --quiet --tags --force origin
git -C "$dest" fetch --quiet origin "$ref" 2>/dev/null || true   # a bare commit id is fetchable on most servers

if target=$(git -C "$dest" rev-parse --verify --quiet "refs/tags/$ref^{commit}" \
    || git -C "$dest" rev-parse --verify --quiet "refs/remotes/origin/$ref^{commit}" \
    || git -C "$dest" rev-parse --verify --quiet "$ref^{commit}"); then
  :
else
  echo "agent-rules: ref '$ref' not found in $url" >&2; exit 1
fi
git -C "$dest" checkout --quiet --detach --force "$target"

(cd "$dest" && ./install.sh)

changed=false
[[ $before != "$target" ]] && changed=true
echo "rules=${target:0:7} changed=$changed"
