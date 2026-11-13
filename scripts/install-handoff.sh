#!/usr/bin/env bash
#
# Install or refresh the pinned herdr-handoff dependency (lock/upstreams.yaml).
#
# herdr-handoff is a REQUIRED operational dependency: `herdr-eng` hands sessions to it, and the
# `/herdr::*` commands in every harness come from its skill tree.  It used to be installed by an inline
# block in scripts/install.sh that carried its own hardcoded default revision, while lock/upstreams.yaml
# declared a different one - so a machine could be "fully installed" behind the declared revision and no
# step would notice.  This script is the single place that resolves the pin, fetches it, installs the CLI
# and links the skills, so installing and updating cannot disagree.
#
# Usage:
#   scripts/install-handoff.sh                  install/refresh the pinned revision (CLI + skills + PATH)
#   scripts/install-handoff.sh --plan           print what WOULD be used; touch nothing
#   scripts/install-handoff.sh --skills-only    skip the CLI install (a CLI installed with uv is still
#                                               linked into PATH by step 4)
#   scripts/install-handoff.sh --no-path-link   do not touch ~/.local/bin
#   scripts/install-handoff.sh --migrate-copies replace skill directories that are plain copies of an
#                                               earlier herdr-handoff install, backing each one up first
#
# Environment:
#   HERDR_HANDOFF_REF     revision to install (default: the pin in lock/upstreams.yaml)
#   HERDR_HANDOFF_URL     clone URL   (default: https://github.com/berlinguyinca/herdr-handoff.git)
#   HERDR_HANDOFF_CACHE   checkout location (default: ~/.local/share/herdr-handoff/<ref>)
#   HERDR_HANDOFF_PYTHON  interpreter the CLI is installed into (default: python3 on PATH)
#   HERDR_HANDOFF_SKILL_ROOT / HERDR_HANDOFF_NO_PATH_LINK  passed to / substituted for the defaults below
#
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lock="$repo_root/lock/upstreams.yaml"

PLAN=0 SKILLS_ONLY=0 PATH_LINK=1 MIGRATE=0
for arg in "$@"; do
  case "$arg" in
    --plan) PLAN=1 ;;
    --skills-only) SKILLS_ONLY=1 ;;
    --no-path-link) PATH_LINK=0 ;;
    --migrate-copies) MIGRATE=1 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) printf 'unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

# The lock file is the source of truth; an override still wins so a controlled rollout can point at a
# branch head or a specific fix without editing the repo.
pinned="$(awk '/^  herdr-handoff:/{inside=1} inside && /revision:/{gsub(/[",]/,"",$2); print $2; exit}' "$lock" 2>/dev/null || true)"
if [ -z "${HERDR_HANDOFF_REF:-}" ]; then
  [ -n "$pinned" ] || { printf 'cannot read the herdr-handoff revision from %s\n' "$lock" >&2; exit 1; }
  HERDR_HANDOFF_REF="$pinned"
fi
UPSTREAM="${HERDR_HANDOFF_URL:-https://github.com/berlinguyinca/herdr-handoff.git}"
CACHE="${HERDR_HANDOFF_CACHE:-$HOME/.local/share/herdr-handoff/$HERDR_HANDOFF_REF}"
PYTHON="${HERDR_HANDOFF_PYTHON:-python3}"
LINK_DIR="${HERDR_LOCAL_BIN:-$HOME/.local/bin}"
if [ "${HERDR_HANDOFF_NO_PATH_LINK:-0}" = 1 ]; then PATH_LINK=0; fi

if [ "$PLAN" = 1 ]; then
  printf 'pin: %s\nupstream: %s\ncache: %s\npython: %s\nskills: %s\npath_link: %s\n' \
    "$HERDR_HANDOFF_REF" "$UPSTREAM" "$CACHE" "$PYTHON" \
    "${HERDR_HANDOFF_SKILL_ROOT:-~/{.codex,.claude,.pi/agent,.config/opencode}/skills}" \
    "$([ "$PATH_LINK" = 1 ] && printf '%s' "$LINK_DIR" || printf 'skipped')"
  exit 0
fi

command -v git >/dev/null 2>&1 || { printf 'git is required to install herdr-handoff\n' >&2; exit 1; }

# --- 1. the pinned checkout, fetched exactly ----------------------------------------------------------
mkdir -p "$(dirname "$CACHE")"
if [ ! -d "$CACHE/.git" ]; then
  printf 'cloning %s at %s\n' "$UPSTREAM" "$HERDR_HANDOFF_REF"
  git clone --quiet "$UPSTREAM" "$CACHE"
fi
have="$(git -C "$CACHE" rev-parse -q --verify "$HERDR_HANDOFF_REF^{commit}" 2>/dev/null || true)"
if [ -z "$have" ]; then
  git -C "$CACHE" fetch --quiet --depth 1 origin "$HERDR_HANDOFF_REF" \
    || git -C "$CACHE" fetch --quiet origin "$HERDR_HANDOFF_REF" \
    || { printf 'cannot fetch herdr-handoff %s from %s\n' "$HERDR_HANDOFF_REF" "$UPSTREAM" >&2; exit 1; }
  have="$(git -C "$CACHE" rev-parse -q --verify "$HERDR_HANDOFF_REF^{commit}" 2>/dev/null \
          || { printf 'fetched, but %s is not a commit in %s\n' "$HERDR_HANDOFF_REF" "$CACHE" >&2; exit 1; })"
fi
if [ "$(git -C "$CACHE" rev-parse HEAD)" = "$have" ]; then
  printf 'already at %s (%s)\n' "$have" "$CACHE"
else
  git -C "$CACHE" checkout --quiet --detach "$have"
  printf 'moved %s to %s\n' "$CACHE" "$have"
fi
# Report what was actually resolved: "installed" without a revision is how a machine ends up quietly
# behind the declared pin.
printf 'pin: %s\nresolved: %s\ncache: %s\n' "$HERDR_HANDOFF_REF" "$have" "$CACHE"
[ -f "$CACHE/install/install.sh" ] || { printf '%s has no install/install.sh\n' "$CACHE" >&2; exit 1; }

# --- 2. the CLI, into the interpreter the caller names -------------------------------------------------
if [ "$SKILLS_ONLY" = 0 ]; then
  # The macOS hosts this runs on carry a Homebrew python that is externally managed and keep tools under
  # ~/.local/share/uv/tools, so a refused pip is a detour there, not a reason to lose the install.
  installed=0
  if "$PYTHON" -m pip --version >/dev/null 2>&1; then
    if pip_report="$("$PYTHON" -m pip install --quiet --upgrade "$CACHE" 2>&1)"; then
      printf 'cli: pip install --upgrade %s (%s)\n' "$CACHE" "$PYTHON"
      installed=1
    fi
  else
    # keep the reason pip is unusable; "no pip" and "externally-managed-environment" send the operator
    # to different fixes.
    pip_report="$("$PYTHON" -m pip --version 2>&1 || true)"
    [ -n "$pip_report" ] || pip_report="$PYTHON has no pip module"
  fi
  if [ "$installed" = 0 ] && command -v uv >/dev/null 2>&1; then
    printf 'pip did not install it (%s); using uv\n' "$(printf '%s' "$pip_report" | tail -n 1)" >&2
    uv tool install --force --reinstall "$CACHE" >/dev/null
    printf 'cli: uv tool install --force %s\n' "$CACHE"
    installed=1
  fi
  if [ "$installed" = 0 ]; then
    printf '%s\n' "$pip_report" >&2
    printf 'no usable installer: pip failed and no uv on PATH. Install the CLI with your own tooling\n' >&2
    printf 'and re-run this with --skills-only.\n' >&2
    exit 1
  fi
fi

# --- 3. the skills, linked into every harness ---------------------------------------------------------
if [ "$MIGRATE" = 1 ] && [ -z "${HERDR_HANDOFF_SKILL_ROOT:-}" ]; then
  # A plain directory is an install from before the linker (or a copy someone made by hand).  Move it
  # aside with its revision recorded, then let the upstream installer link the pinned tree in its place.
  # Only the two skills this repo owns are considered, and only when the file identifies itself.
  for dir in "${CODEX_HOME:-$HOME/.codex}/skills" "${CLAUDE_SKILLS_DIR:-$HOME/.claude}/skills" \
             "${PI_SKILLS_DIR:-$HOME/.pi/agent}/skills" "${OPENCODE_SKILLS_DIR:-$HOME/.config/opencode}/skills"; do
    for skill in herdr herdr-handoff; do
      destination="$dir/$skill"
      [ -d "$destination" ] && [ ! -L "$destination" ] || continue
      grep -q "^name: $skill$" "$destination/SKILL.md" 2>/dev/null || continue
      backup="$destination.pre-$HERDR_HANDOFF_REF.bak"
      mv "$destination" "$backup"
      printf 'moved aside %s -> %s\n' "$destination" "$backup"
    done
  done
fi
skill_report="$("${SHELL:-/bin/sh}" "$CACHE/install/install.sh" 2>&1)" || {
  printf '%s\n' "$skill_report" >&2; exit 1; }
printf 'skills:\n%s\n' "$skill_report"
case "$skill_report" in
  *collision*)
    printf '  a skill above belongs to something else (or predates the linker); herdr-eng keeps the old\n'
    printf '  skill text until it is resolved: scripts/install-handoff.sh --migrate-copies backs each one\n'
    printf '  up and links the pinned tree in its place.\n' ;;
esac

# --- 4. PATH ------------------------------------------------------------------------------------------------
if [ "$PATH_LINK" = 1 ]; then
  # Linking the entrypoint that already IS the PATH entry turns it into a self-referencing symlink
  # (`Too many levels of symbolic links`), which is how this ran once and broke a working uv tool shim.
  # So a candidate is only linked when it is not the destination, and success is stated only when a link
  # was actually made.
  destination="$LINK_DIR/herdr-handoff"
  if command -v herdr-handoff >/dev/null 2>&1 && [ "$(command -v herdr-handoff)" != "$destination" ]; then
    printf 'cli on PATH: %s (%s)\n' "$(command -v herdr-handoff)" "$(herdr-handoff --version 2>/dev/null || echo unknown)"
  else
    linked=""
    for candidate in "$(dirname "$PYTHON")/herdr-handoff" \
                     "$HOME/.local/share/uv/tools/herdr-handoff/bin/herdr-handoff"; do
      [ -x "$candidate" ] || continue
      [ "$(cd "$(dirname "$candidate")" && printf '%s/%s\n' "$(pwd)" "$(basename "$candidate")")" = "$destination" ] && continue
      if [ -d "$LINK_DIR" ] || mkdir -p "$LINK_DIR" 2>/dev/null; then
        ln -sf "$candidate" "$destination" && linked="$candidate" && break
      fi
    done
    if [ -n "$linked" ]; then
      printf 'path: %s -> %s\n' "$destination" "$linked"
    else
      printf 'no separate entrypoint to link into %s; leaving PATH alone\n' "$LINK_DIR" >&2
    fi
  fi
fi
