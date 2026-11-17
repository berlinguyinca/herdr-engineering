#!/usr/bin/env bash
# Usage: install-screenwright.sh
# Install/refresh berlinguyinca/screenwright, deliberately TRACKING origin/main (not a pinned sha), and link its
# screenwright-review skill into the harness skill dirs. Prints a final machine-readable line:
#   screenwright=<short sha> changed=<true|false>
# First run clones to $HERDR_SCREENWRIGHT_DIR; later runs `git fetch` and fast-forward to origin/main. A dirty
# checkout, a checkout on another branch, or a diverged main is left untouched: the script says so and exits 0
# (the final line then reports changed=false), like the fleet auto-updater. A failed clone/fetch exits non-zero.
#
# Env:
#   HERDR_SCREENWRIGHT_URL   upstream (default git@github.com:berlinguyinca/screenwright.git)
#   HERDR_SCREENWRIGHT_DIR   checkout (default ~/.local/share/herdr-engineering/screenwright)
#   HERDR_SCREENWRIGHT_REF   pin to a tag/sha (detached) instead of tracking main
#   HERDR_SCREENWRIGHT_NPM=0 skip `npm install` (the dependency step the repo's scripts/sw tells you to run)
#   HERDR_INSTALL_SCREENWRIGHT=0  do nothing
set -euo pipefail

[ "${HERDR_INSTALL_SCREENWRIGHT:-1}" = "0" ] && { echo "screenwright: skipped (HERDR_INSTALL_SCREENWRIGHT=0)" >&2; exit 0; }

# Non-interactive ssh: never prompt for a password or a host key. An operator-provided GIT_SSH_COMMAND wins.
: "${GIT_SSH_COMMAND:=ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new}"
export GIT_SSH_COMMAND GIT_TERMINAL_PROMPT=0

URL="${HERDR_SCREENWRIGHT_URL:-git@github.com:berlinguyinca/screenwright.git}"
DEST="${HERDR_SCREENWRIGHT_DIR:-$HOME/.local/share/herdr-engineering/screenwright}"
REF="${HERDR_SCREENWRIGHT_REF:-}"
SKILL=screenwright-review

say() { printf 'screenwright: %s\n' "$*" >&2; }
finish() { printf 'screenwright=%s changed=%s\n' "$(git -C "$DEST" rev-parse --short HEAD 2>/dev/null || echo none)" "$1"; }

before=none
if [[ -d $DEST/.git ]]; then
  before=$(git -C "$DEST" rev-parse HEAD)
  if [[ -n $(git -C "$DEST" status --porcelain) ]]; then
    say "checkout $DEST is dirty; leaving it untouched"; finish false; exit 0
  fi
  if [[ -z $REF && $(git -C "$DEST" branch --show-current) != main ]]; then
    say "checkout $DEST is on '$(git -C "$DEST" branch --show-current)', not main; leaving it untouched"; finish false; exit 0
  fi
  git -C "$DEST" remote set-url origin "$URL"
  git -C "$DEST" fetch --quiet --tags --force origin
else
  mkdir -p "$(dirname "$DEST")"
  git clone --quiet -- "$URL" "$DEST"
fi

if [[ -n $REF ]]; then
  git -C "$DEST" checkout --quiet --detach "$REF"
elif ! git -C "$DEST" merge --quiet --ff-only origin/main 2>/dev/null; then
  say "main cannot fast-forward to origin/main (diverged); leaving it untouched"; finish false; exit 0
fi

after=$(git -C "$DEST" rev-parse HEAD)
changed=false; [[ $before != "$after" ]] && changed=true

# Dependencies: scripts/sw reports "run: npm install" when node_modules/.bin/tsx is missing. Failure only warns.
if [[ ${HERDR_SCREENWRIGHT_NPM:-1} != 0 && -f $DEST/package.json ]] && { [[ $changed = true ]] || [[ ! -x $DEST/node_modules/.bin/tsx ]]; }; then
  (cd "$DEST" && npm install --no-audit --no-fund --loglevel=error) >&2 || say "WARNING: npm install failed; run it by hand in $DEST"
fi

# The repo owns its skill linking (canonical ~/.agents/skills + the four harness dirs); a conflict only warns.
ensure="$DEST/skills/$SKILL/scripts/ensure-install.sh"
if [[ -x $ensure ]]; then
  "$ensure" >&2 || say "WARNING: $SKILL was not fully linked (see above); existing files are never overwritten"
else
  say "WARNING: $ensure not found; skill not linked"
fi

finish "$changed"
