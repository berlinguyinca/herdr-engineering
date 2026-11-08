#!/usr/bin/env bash
# Usage: agent-rules-sync.sh <repo-url> <ref> <dest>
#        agent-rules-sync.sh --validate-ref <ref>      (exit 0 valid, 2 not)
#        agent-rules-sync.sh --print-ssh-command
# Clone/fetch the shared agent-rules repo into <dest>, check out <ref> (detached), run its install.sh, and print a final
#   rules=<short sha> changed=<true|false> install_changed=<true|false>
# line. <ref> is an immutable pin: a TAG or a commit sha (7-40 hex). A branch name is refused, and a name that is both a
# tag and a branch is resolved as the tag (refs/tags/). A missing ref fails and leaves the checkout untouched.
# Idempotent: install.sh runs on every invocation (it is itself idempotent); `changed` tracks HEAD, `install_changed`
# tracks the harness symlinks into the checkout and ~/.claude/CLAUDE.md.
# The ref rules mirror herdr_engineering/control_plane/agent_rules_config.py (a test keeps them in sync).
set -euo pipefail

# Non-interactive ssh: never prompt for a password or a host key (a fresh host has no github.com known_hosts entry).
# An operator-provided GIT_SSH_COMMAND wins.
: "${GIT_SSH_COMMAND:=ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new}"
export GIT_SSH_COMMAND GIT_TERMINAL_PROMPT=0

valid_ref() {
  local r=$1
  [[ ${#r} -le 128 && $r =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || return 1
  case $r in
    *..* | *//* | */ | *. | *.lock | */.* | *'@{'*) return 1 ;;
  esac
}

digest() { if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi; }

# What install.sh is allowed to change: symlinks into the checkout under the harness dirs, and CLAUDE.md (@import line).
snapshot() {
  {
    for d in .claude .codex .config/opencode .pi .gemini .agents; do
      [[ -d $HOME/$d ]] || continue
      find "$HOME/$d" -maxdepth 6 -type l -exec sh -c 'for f; do printf "%s -> %s\n" "$f" "$(readlink "$f")"; done' sh {} + \
        2>/dev/null | grep -F agent-rules || true
    done
    if [[ -f $HOME/.claude/CLAUDE.md ]]; then digest < "$HOME/.claude/CLAUDE.md"; fi
  } | sort | digest
}

case ${1:-} in
  --validate-ref) valid_ref "${2-}" && exit 0 || exit 2 ;;
  --print-ssh-command) echo "$GIT_SSH_COMMAND"; exit 0 ;;
esac

url=${1:?repo url}; ref=${2:?ref}; dest=${3:?dest}
if ! valid_ref "$ref"; then echo "agent-rules: unsafe ref" >&2; exit 2; fi
if [[ $url == -* ]]; then echo "agent-rules: unsafe repo url" >&2; exit 2; fi

before=none
if [[ -d $dest/.git ]]; then
  before=$(git -C "$dest" rev-parse HEAD 2>/dev/null || echo none)
  git -C "$dest" remote set-url origin "$url"
else
  git clone --quiet -- "$url" "$dest"
fi
git -C "$dest" fetch --quiet --tags --force origin
if [[ $ref =~ ^[0-9a-fA-F]{40}$ ]]; then
  git -C "$dest" fetch --quiet origin "$ref" 2>/dev/null || true   # a commit not on any branch is fetchable by id
fi

if target=$(git -C "$dest" rev-parse --verify --quiet "refs/tags/$ref^{commit}"); then
  :                                                                  # a tag always wins over a same-named branch
elif [[ $ref =~ ^[0-9a-fA-F]{7,40}$ ]] && target=$(git -C "$dest" rev-parse --verify --quiet "$ref^{commit}"); then
  :
elif git -C "$dest" rev-parse --verify --quiet "refs/remotes/origin/$ref" >/dev/null; then
  echo "agent-rules: '$ref' is a branch; pin a tag or a commit sha so the rules cannot move underneath the fleet" >&2
  exit 1
else
  echo "agent-rules: ref '$ref' not found as a tag or commit in $url" >&2; exit 1
fi
git -C "$dest" checkout --quiet --detach --force "$target"

snap_before=$(snapshot)
(cd "$dest" && ./install.sh)
snap_after=$(snapshot)

changed=false; [[ $before != "$target" ]] && changed=true
install_changed=false; [[ $snap_before != "$snap_after" ]] && install_changed=true
echo "rules=${target:0:7} changed=$changed install_changed=$install_changed"
