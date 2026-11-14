#!/usr/bin/env bash
#
# Install or refresh the herdr-handoff dependency at the revision herdr-engineering declares.
#
# The revision comes from lock/upstreams.yaml, the same file `herdr-eng fleet release` reads and
# `herdr-eng fleet status` compares against, so install, update and drift reporting cannot disagree
# the way they used to: `scripts/install.sh` carried its own hardcoded default revision and could
# silently install something other than the pinned one.  This script is the only place the pin is
# resolved; both install.sh and `herdr-eng fleet update` call it.
#
# It fetches the pinned tree, runs the upstream installer (which links the `herdr` and `herdr-handoff`
# skills into ~/.codex/skills, ~/.claude/skills, ~/.pi/agent/skills and ~/.config/opencode/skills by
# symlink, never by copy), installs the CLI with pip and falls back to `uv tool install`, and puts the
# entrypoint on PATH.  It never copies skill text: a copy is a revision nobody maintains, and copies
# are why a host running v0.2.0 skills could pass a check for v0.3.0 behaviour.  Nothing here needs a
# venv.
#
# Usage: install-handoff.sh [options]
#   --plan             print the resolved pin, cache, interpreter and skill directories and exit 0
#                      without touching anything
#   --skills-only      link the skills; leave the CLI install and the PATH entry alone
#   --no-path-link     skip the PATH entrypoint (HERDR_HANDOFF_NO_PATH_LINK=1 does the same)
#   --migrate-copies   authorize moving aside what this script would otherwise refuse to touch:
#                      a plain skill directory that predates the linker, and a PATH entry that is a
#                      hand-written file or a link to something we do not manage.  Each is renamed to
#                      <name>.pre-<revision>.bak first; the script refuses rather than nest a backup
#                      inside an existing one
#   --force            redo the CLI install even when the version already matches, and authorize the
#                      PATH collision the same way --migrate-copies does
#
# Environment: HERDR_HANDOFF_REF (override the pin for a controlled rollout), HERDR_HANDOFF_URL,
# HERDR_HANDOFF_REPO (install from a local checkout instead of cloning upstream),
# HERDR_HANDOFF_CACHE, HERDR_HANDOFF_PYTHON, HERDR_LOCAL_BIN, HERDR_HANDOFF_SKILL_ROOT,
# HERDR_HANDOFF_NO_PATH_LINK, CODEX_HOME, CLAUDE_SKILLS_DIR, PI_SKILLS_DIR, OPENCODE_SKILLS_DIR.
set -euo pipefail

PLAN=0
SKILLS_ONLY=0
PATH_LINK=1
MIGRATE=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --plan) PLAN=1 ;;
    --skills-only) SKILLS_ONLY=1 ;;
    --no-path-link) PATH_LINK=0 ;;
    --migrate-copies) MIGRATE=1 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) printf 'unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lock="${HERDR_UPSTREAM_LOCK:-$script_dir/../lock/upstreams.yaml}"

# The lock file is the source of truth; an override still wins so a controlled rollout can point at a
# branch head or a specific fix without editing the repo.
pinned="$(awk '/^  herdr-handoff:/{inside=1} inside && /revision:/{gsub(/[",]/,"",$2); print $2; exit}' "$lock" 2>/dev/null || true)"
if [ -z "${HERDR_HANDOFF_REF:-}" ]; then
  [ -n "$pinned" ] || { printf 'cannot read the herdr-handoff revision from %s\n' "$lock" >&2; exit 1; }
  HERDR_HANDOFF_REF="$pinned"
fi
REF="$HERDR_HANDOFF_REF"
UPSTREAM="${HERDR_HANDOFF_URL:-https://github.com/berlinguyinca/herdr-handoff.git}"
REPO="${HERDR_HANDOFF_REPO:-}"
CACHE="${HERDR_HANDOFF_CACHE:-$HOME/.local/share/herdr-handoff/$REF}"
LINK_DIR="${HERDR_LOCAL_BIN:-$HOME/.local/bin}"
if [ "${HERDR_HANDOFF_NO_PATH_LINK:-0}" = 1 ]; then PATH_LINK=0; fi

# An interpreter named as a bare word (`python3`) makes every derived path relative:
# `dirname python3` is `.`, so the entrypoint candidate becomes `./herdr-handoff`, is never found, and
# the script used to exit 0 having linked nothing.  Resolve it to the binary that will really run.
PYTHON="${HERDR_HANDOFF_PYTHON:-python3}"
if command -v "$PYTHON" >/dev/null 2>&1; then PYTHON="$(command -v "$PYTHON")"; fi

# The four harness skill directories, honouring the per-harness overrides.  `CLAUDE_SKILLS_DIR`,
# `PI_SKILLS_DIR` and `OPENCODE_SKILLS_DIR` name the skills directory itself (that is what the upstream
# installer treats them as); only `CODEX_HOME` names a home whose `skills` child is used.
skill_dirs() {
  if [ -n "${HERDR_HANDOFF_SKILL_ROOT:-}" ]; then
    printf '%s\n' "$HERDR_HANDOFF_SKILL_ROOT"
    return
  fi
  printf '%s\n' "${CODEX_HOME:-$HOME/.codex}/skills" "${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}" \
    "${PI_SKILLS_DIR:-$HOME/.pi/agent/skills}" "${OPENCODE_SKILLS_DIR:-$HOME/.config/opencode/skills}"
}

if [ "$PLAN" = 1 ]; then
  printf 'pin: %s\nupstream: %s\nrepo: %s\ncache: %s\npython: %s\npath_link: %s\nskills:\n' \
    "$REF" "$UPSTREAM" "${REPO:-<clone upstream>}" "$CACHE" "$PYTHON" \
    "$([ "$PATH_LINK" = 1 ] && printf '%s' "$LINK_DIR" || printf 'skipped')"
  skill_dirs | sed 's/^/  /'
  exit 0
fi

# --- 1. the pinned checkout, fetched exactly ----------------------------------------------------------
TREE="$REPO"
if [ -n "$REPO" ]; then
  [ -d "$REPO" ] || { printf '%s is not a directory\n' "$REPO" >&2; exit 1; }
  [ -f "$REPO/install/install.sh" ] || { printf '%s has no install/install.sh\n' "$REPO" >&2; exit 1; }
  # A local checkout is honoured as-is, so say which revision it actually is: "installed from a local
  # repo" without a SHA is how a machine ends up quietly behind the pin.
  have="$(git -C "$REPO" rev-parse -q --verify HEAD^{commit} 2>/dev/null || printf unknown)"
  printf 'pin: %s\nresolved: %s\nrepo: %s\n' "$REF" "$have" "$REPO"
  [ "$have" = "$REF" ] || printf '  warning: %s is not the pinned revision %s\n' "$have" "$REF" >&2
else
  command -v git >/dev/null 2>&1 || { printf 'git is required to install herdr-handoff\n' >&2; exit 1; }
  mkdir -p "$(dirname "$CACHE")"
  if [ ! -d "$CACHE/.git" ]; then
    printf 'cloning %s at %s\n' "$UPSTREAM" "$REF"
    git clone --quiet "$UPSTREAM" "$CACHE"
  fi
  TREE="$CACHE"
  # A full SHA is immutable, so a cached copy of it is already correct.  A branch name is not: the ref
  # sitting in the cache is whatever was fetched the last time, so a branch-head override must be
  # re-fetched on every run or it installs a stale commit forever.
  if printf '%s' "$REF" | grep -qE '^[0-9a-fA-F]{40}$'; then
    have="$(git -C "$CACHE" rev-parse -q --verify "$REF^{commit}" 2>/dev/null || true)"
  else
    have=""
  fi
  if [ -z "$have" ]; then
    git -C "$CACHE" fetch --quiet --depth 1 origin "$REF" \
      || git -C "$CACHE" fetch --quiet origin "$REF" \
      || { printf 'cannot fetch herdr-handoff %s from %s\n' "$REF" "$UPSTREAM" >&2; exit 1; }
    # Ask FETCH_HEAD first.  A fetch of a branch updates FETCH_HEAD; the same name may also resolve to a
    # stale local branch in the cache, and preferring that made a branch-head override install the commit
    # fetched the first time, forever.
    have="$(git -C "$CACHE" rev-parse -q --verify 'FETCH_HEAD^{commit}' 2>/dev/null \
            || git -C "$CACHE" rev-parse -q --verify "$REF^{commit}" 2>/dev/null || true)"
    [ -n "$have" ] || { printf 'fetched, but %s is not a commit in %s\n' "$REF" "$CACHE" >&2; exit 1; }
  fi
  if [ "$(git -C "$CACHE" rev-parse HEAD)" = "$have" ]; then
    printf 'already at %s (%s)\n' "$have" "$CACHE"
  else
    git -C "$CACHE" checkout --quiet --detach "$have"
    printf 'moved %s to %s\n' "$CACHE" "$have"
  fi
  # Report what was actually resolved: "installed" without a revision is how a machine ends up quietly
  # behind the declared pin.
  printf 'pin: %s\nresolved: %s\ncache: %s\n' "$REF" "$have" "$CACHE"
fi
[ -f "$TREE/install/install.sh" ] || { printf '%s has no install/install.sh\n' "$TREE" >&2; exit 1; }

# The version the pinned tree declares, used at the end to attest that the CLI on PATH is this
# revision and not an older one that happens to be on PATH.
expected_version="$(sed -n 's/^version = "\([^"]*\)".*/\1/p' "$TREE/pyproject.toml" 2>/dev/null | head -n 1)"

# --- 2. the CLI, into the interpreter the caller names -------------------------------------------------
if [ "$SKILLS_ONLY" = 0 ]; then
  # The macOS hosts this runs on carry a Homebrew python that is externally managed and keep tools under
  # ~/.local/share/uv/tools, so a refused pip is a detour there, not a reason to lose the install.
  installed=0
  if "$PYTHON" -m pip --version >/dev/null 2>&1; then
    if pip_report="$("$PYTHON" -m pip install --quiet --upgrade "$TREE" 2>&1)"; then
      printf 'cli: pip install --upgrade %s (%s)\n' "$TREE" "$PYTHON"
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
    uv tool install --force --reinstall "$TREE" >/dev/null
    printf 'cli: uv tool install --force %s\n' "$TREE"
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
if [ "$MIGRATE" = 1 ]; then
  # A plain directory is an install from before the linker (or a copy someone made by hand).  Move it
  # aside with its revision recorded, then let the upstream installer link the pinned tree in its place.
  # Only the two skills this repo owns are considered, and only when the file identifies itself.
  while IFS= read -r dir; do
    for skill in herdr herdr-handoff; do
      destination="$dir/$skill"
      [ -d "$destination" ] && [ ! -L "$destination" ] || continue
      grep -q "^name: $skill$" "$destination/SKILL.md" 2>/dev/null || continue
      backup="$destination.pre-$REF.bak"
      # `mv dir existing_dir` nests the copy inside the old backup and looks like a success.  Refuse
      # instead: two revisions of hand-written skill text are not something to merge silently.
      if [ -e "$backup" ]; then printf 'refusing to move %s aside: %s already exists\n' "$destination" "$backup" >&2; exit 1; fi
      mv "$destination" "$backup"
      printf 'moved aside %s -> %s\n' "$destination" "$backup"
    done
  done < <(skill_dirs)
fi
# The upstream installer is POSIX sh; running it through $SHELL makes a fish or csh login shell fail it.
skill_report="$(/bin/sh "$TREE/install/install.sh" 2>&1)" || { printf '%s\n' "$skill_report" >&2; exit 1; }
printf 'skills:\n%s\n' "$skill_report"
case "$skill_report" in
  *collision*)
    printf '  a skill above belongs to something else (or predates the linker); herdr-eng keeps the old\n'
    printf '  skill text until it is resolved: scripts/install-handoff.sh --migrate-copies backs each one\n'
    printf '  up and links the pinned tree in its place.\n' ;;
esac

# --- 4. PATH: make the entrypoint reachable, and prove we did -----------------------------------------
# --skills-only says "leave the CLI install and the PATH entry alone", so this whole step is skipped:
# an operator pointing the PATH entry somewhere deliberate is not something this script should judge.
if [ "$PATH_LINK" = 1 ] && [ "$SKILLS_ONLY" = 0 ]; then
  destination="$LINK_DIR/herdr-handoff"
  # Entrypoints the installs above can produce.  Each is normalized before comparison: linking the
  # entrypoint that already IS the PATH entry turns it into a self-referencing symlink
  # (`Too many levels of symbolic links`), which is how this ran once and broke a working uv tool shim.
  # `dirname` of a bare `python3` is `.`; the interpreter is resolved above so this stays absolute.
  uv_shim="$HOME/.local/share/uv/tools/herdr-handoff/bin/herdr-handoff"
  normalize() { # absolute path of an existing file, without needing realpath on older macOS
    (cd "$(dirname "$1")" && printf '%s/%s\n' "$(pwd -P)" "$(basename "$1")")
  }
  ours() { # whether a path is one of the entrypoints this script installs
    case "$1" in
      "$(dirname "$PYTHON")/herdr-handoff"|"$uv_shim"|"$CACHE"/*) return 0 ;;
      *) return 1 ;;
    esac
  }
  # Where the entrypoint already sits, normalized.  An entrypoint that this script's own installs put
  # here (pip into <python>/bin when HERDR_LOCAL_BIN is that bin dir, or a uv shim when it is the uv
  # tool's bin) is ours, and is attested below rather than treated as somebody else's file.
  destination_abs="$destination"
  { [ -e "$destination" ] || [ -L "$destination" ]; } && destination_abs="$(normalize "$destination")"
  ours_here=0
  ours "$destination_abs" && ours_here=1

  chosen=""
  for candidate in "$(dirname "$PYTHON")/herdr-handoff" "$HOME/.local/bin/herdr-handoff" "$uv_shim"; do
    [ -x "$candidate" ] || continue
    absolute="$(normalize "$candidate")"
    # Installing here put the entrypoint in the link directory already; linking it onto itself is the
    # self-referencing symlink that made a working CLI answer "Too many levels of symbolic links".
    if [ "$absolute" = "$destination_abs" ]; then continue; fi
    ours "$absolute" || continue
    chosen="$absolute"
    break
  done

  if [ "$ours_here" = 0 ] && [ -e "$destination" ] && [ ! -L "$destination" ]; then
    # A regular file or directory where the entrypoint belongs is hand-written, or another tool's.  It
    # used to be overwritten by `ln -sf` with no backup and no message; that is the loss this script must
    # never cause, so it is moved aside only when the caller says so.
    if [ "$MIGRATE" = 1 ] || [ "$FORCE" = 1 ]; then
      backup="$destination.pre-$REF.bak"
      if [ -e "$backup" ]; then printf 'refusing to replace %s: %s already exists\n' "$destination" "$backup" >&2; exit 1; fi
      mv "$destination" "$backup"
      printf 'moved aside %s -> %s\n' "$destination" "$backup"
    else
      printf 'collision: %s is a %s this script does not manage and will not overwrite.\n' \
        "$destination" "$([ -d "$destination" ] && printf directory || printf file)" >&2
      printf '  Remove it, point HERDR_LOCAL_BIN somewhere else, or re-run with --migrate-copies to back\n' >&2
      printf '  it up as %s.pre-%s.bak\n' "$destination" "$REF" >&2
      exit 2
    fi
  elif [ "$ours_here" = 0 ] && [ -L "$destination" ]; then
    target="$(readlink "$destination")"
    absolute="$target"
    case "$target" in /*) : ;; *) absolute="$(normalize "$LINK_DIR/$target")" ;; esac
    if ! ours "$absolute"; then
      # A symlink pointing at something we do not install is a deliberate choice by someone else; the
      # same rule applies.  (Comparing the link itself, not its target, is what made this fire on links
      # this script made itself.)
      target_abs="$absolute"
      if [ "$MIGRATE" = 1 ] || [ "$FORCE" = 1 ]; then
        backup="$destination.pre-$REF.bak"
        if [ -e "$backup" ]; then printf 'refusing to replace %s: %s already exists\n' "$destination" "$backup" >&2; exit 1; fi
        mv "$destination" "$backup"
        printf 'moved aside %s -> %s\n' "$destination" "$backup"
      else
        printf 'collision: %s links to %s, which this script does not manage.\n' "$destination" "$target_abs" >&2
        printf '  Re-run with --migrate-copies to back it up and link the pinned entrypoint instead.\n' >&2
        exit 2
      fi
    fi
  fi

  if [ -n "$chosen" ]; then
    mkdir -p "$LINK_DIR"
    ln -sf "$chosen" "$destination"
    printf 'path: %s -> %s\n' "$destination" "$chosen"
  elif [ -e "$destination" ] || [ -L "$destination" ]; then
    printf 'path: %s already resolves; nothing linked\n' "$destination"
  else
    printf 'no entrypoint to link into %s (%s is not installed there)\n' "$LINK_DIR" "$PYTHON" >&2
    printf '  Set HERDR_HANDOFF_PYTHON to the interpreter the CLI was installed into, or HERDR_LOCAL_BIN\n' >&2
    printf '  to a directory that already has one.\n' >&2
    exit 1
  fi

  # Attest the thing we just claimed is reachable.  "leaving PATH alone" plus exit 0 used to report
  # success while the CLI on PATH stayed on the previous revision; comparing the version the pinned tree
  # declares against the version the entrypoint reports makes that impossible to report as success.
  if [ -z "$expected_version" ]; then
    printf 'cannot read the version from %s/pyproject.toml; not attesting the CLI\n' "$TREE" >&2
  else
    reported="$( "$destination" --version 2>/dev/null | head -n 1 || true)"
    if [ "$reported" != "$expected_version" ]; then
      printf 'PATH does not match the pin: %s reports %s, %s declares %s\n' \
        "$destination" "${reported:-<no version>}" "$TREE" "$expected_version" >&2
      printf '  Refresh did not converge.  Check HERDR_HANDOFF_PYTHON and that %s is on PATH.\n' "$LINK_DIR" >&2
      exit 1
    fi
    printf 'attested: %s reports %s (pin %s)\n' "$destination" "$reported" "$REF"
  fi
fi
