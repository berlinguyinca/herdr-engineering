#!/usr/bin/env bash
# Install or update the herdr-handoff dependency: the CLI at the pinned revision, plus the `herdr` and
# `herdr-handoff` skills linked into every harness that runs an agent on this machine.
#
# This script is the only place the revision of that dependency is resolved.  It used to be declared in
# two places (lock/upstreams.yaml, and a hardcoded default in scripts/install.sh), which let a host report
# a complete install while running a revision older than the one `herdr-eng fleet status` reported against.
#
# Upstream, the CLI and the skills are two halves of one install: `pip install .` gives you the executable,
# `install/install.sh` symlinks the skills into ~/.codex/skills, ~/.claude/skills, ~/.pi/agent/skills and
# ~/.config/opencode/skills.  Calling only the first half - which is what this repo used to do - leaves
# every agent with no skill to read while appearing to have installed the dependency.
#
# Linking is what makes the install updatable.  A copied skill directory never notices a new revision; a
# symlink into ~/.local/share/herdr-handoff/<revision> moves when this script fetches one.
#
# Convergence is attested, not assumed: the script ends by comparing the version the pinned tree declares
# against the version the entrypoint on PATH reports, and fails if they differ.  Reporting success while an
# older CLI stays on PATH is the failure mode that made this dependency invisible before, and a symlink is
# only proof of intent, not of which program runs.
#
# Usage: install-handoff.sh [--plan] [--skills-only] [--no-path-link] [--migrate-copies] [--force]
#
#   --plan             print the pin, the resolved cache, the interpreter, the PATH directory and the skill
#                      directories it would write, then exit 0 without touching anything
#   --skills-only      link the skills; leave the CLI install and the PATH entry alone
#   --no-path-link     skip the PATH entrypoint (HERDR_HANDOFF_NO_PATH_LINK=1 does the same)
#   --migrate-copies   authorize moving aside what this script would otherwise refuse to touch: a plain
#                      skill directory that predates the linker, and a PATH entry that neither is the pin
#                      nor came from this script.  Each is renamed to <name>.pre-<revision>.bak first; the
#                      script refuses rather than nest a backup inside an existing one
#   --force            authorize the PATH collision the same way --migrate-copies does
#
# Environment: HERDR_HANDOFF_REF (override the pin for a controlled rollout), HERDR_HANDOFF_URL,
# HERDR_HANDOFF_REPO (install from a local checkout instead of cloning upstream), HERDR_HANDOFF_CACHE,
# HERDR_HANDOFF_PYTHON, HERDR_LOCAL_BIN, HERDR_HANDOFF_NO_PATH_LINK, CODEX_HOME, CLAUDE_SKILLS_DIR,
# PI_SKILLS_DIR, OPENCODE_SKILLS_DIR.  HERDR_HANDOFF_SKILL_ROOT means the same thing here as it does
# upstream: the directory whose codex/, claude/, pi/ and opencode/ children hold the skills.
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
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
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

# An interpreter named as a bare word (`python3`) makes every derived path relative: `dirname python3` is
# `.`, so the entrypoint candidate becomes `./herdr-handoff`, is never found, and the script used to exit 0
# having linked nothing.  Resolve it to the binary that will really run.
PYTHON="${HERDR_HANDOFF_PYTHON:-python3}"
if command -v "$PYTHON" >/dev/null 2>&1; then PYTHON="$(command -v "$PYTHON")"; fi

# Absolute paths with every symlink resolved, including the last component.  `/tmp` and `/var` are symlinks
# on macOS, and pytest's tmp_path is not, so a comparison that resolved only the parent directory looked
# fine in tests and failed on a fresh host: the entrypoint pip had just installed did not match the path
# this script believed its own, and nothing got linked.
normalize() {
  if [ -e "$1" ] || [ -L "$1" ]; then
    (cd "$(dirname "$1")" 2>/dev/null && printf '%s/%s\n' "$(pwd -P)" "$(basename "$1")") && return 0
  fi
  printf '%s\n' "$1"
}

# The four harness skill directories.  HERDR_HANDOFF_SKILL_ROOT is the parent of the per-harness dirs,
# exactly as upstream's installer treats it; otherwise the per-harness variables apply, and
# CLAUDE_SKILLS_DIR / PI_SKILLS_DIR / OPENCODE_SKILLS_DIR name the skills directory itself while CODEX_HOME
# names a home whose `skills` child is used.
skill_dirs() {
  if [ -n "${HERDR_HANDOFF_SKILL_ROOT:-}" ]; then
    printf '%s\n' "$HERDR_HANDOFF_SKILL_ROOT/codex/skills" "$HERDR_HANDOFF_SKILL_ROOT/claude/skills" \
      "$HERDR_HANDOFF_SKILL_ROOT/pi/skills" "$HERDR_HANDOFF_SKILL_ROOT/opencode/skills"
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
  have="$(git -C "$REPO" rev-parse -q --verify 'HEAD^{commit}' 2>/dev/null || printf unknown)"
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

# The version the pinned tree declares is what the entrypoint on PATH is attested against at the end.  If
# it cannot be read there is nothing to attest against, so the script stops here rather than exiting 0 with
# an unverified CLI - that is the same hole this whole step exists to close.
expected_version="$(sed -n 's/^version = "\([^"]*\)".*/\1/p' "$TREE/pyproject.toml" 2>/dev/null | head -n 1)"
if [ -z "$expected_version" ] && [ "$SKILLS_ONLY" = 0 ]; then
  printf 'cannot read a static version from %s/pyproject.toml, so the CLI cannot be attested\n' "$TREE" >&2
  exit 1
fi

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
    # Ask uv to build the tool environment on the same interpreter the caller named.  Without --python uv
    # picks one of its own, and the report would claim an interpreter that had nothing to do with the
    # install.  uv still keeps the tool in its own venv (~/.local/share/uv/tools/<tool>); what binds the
    # result to this pin is the attestation at the end, not a directory.
    uv tool install --python "$PYTHON" --force --reinstall "$TREE" >/dev/null
    printf 'cli: uv tool install --python %s --force --reinstall %s\n' "$PYTHON" "$TREE"
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
# It is also the only thing that knows the real skill layout, so it is run rather than reimplemented.
skill_report="$(/bin/sh "$TREE/install/install.sh" 2>&1)" || { printf '%s\n' "$skill_report" >&2; exit 1; }
printf 'skills:\n%s\n' "$skill_report"
case "$skill_report" in
  *collision*)
    printf '  a skill above belongs to something else (or predates the linker); herdr-eng keeps the old\n'
    printf '  skill text until it is resolved: scripts/install-handoff.sh --migrate-copies backs each one\n'
    printf '  up and links the pinned tree in its place.\n' ;;
esac

# --- 4. PATH: make the entrypoint reachable, and prove we did -----------------------------------------
# --skills-only says "leave the CLI install and the PATH entry alone", so this whole step is skipped: an
# operator pointing the PATH entry somewhere deliberate is not something this script should judge.
if [ "$PATH_LINK" = 1 ] && [ "$SKILLS_ONLY" = 0 ]; then
  destination="$LINK_DIR/herdr-handoff"
  python_bin="$(normalize "$(dirname "$PYTHON")")"
  uv_shim="$(normalize "$HOME/.local/share/uv/tools/herdr-handoff/bin/herdr-handoff")"
  cache_root="$(normalize "$CACHE")"
  # Entrypoints the installs above can produce, compared resolved-to-resolved.  Linking the entrypoint that
  # already IS the PATH entry turns it into a self-referencing symlink (`Too many levels of symbolic
  # links`), which is how this ran once against a real ~/.local/bin and left a working CLI broken.
  ours() {
    case "$1" in
      "$python_bin/herdr-handoff"|"$uv_shim"|"$cache_root"/*) return 0 ;;
      *) return 1 ;;
    esac
  }
  # Whether what is on PATH right now already reports the pinned version.  This runs the entrypoint - the
  # same command the attestation below runs - because a symlink, a directory name and a pip log are all
  # evidence of intent, and none of them is evidence of which program runs.
  reports_the_pin() {
    [ -x "$destination" ] || return 1
    [ "$( "$destination" --version 2>/dev/null | head -n 1 | tr -d '[:space:]' || true)" = "$expected_version" ]
  }
  # Where an entrypoint this install produced already sits.  pip into a prefix whose bin *is* LINK_DIR, and
  # the uv shim when LINK_DIR is the uv tool's bin, both land exactly on $destination: those are this
  # script's own files, and on a Linux box a pip `--user` install lands there every single run.
  chosen=""
  for candidate in "$python_bin/herdr-handoff" "$uv_shim" "$CACHE/herdr-handoff"; do
    [ -x "$candidate" ] && { chosen="$candidate"; break; }
  done
  destination_is_ours() { # whether what sits on PATH came from this script's own installs
    local probe="${1:-$destination}"
    if [ -L "$destination" ]; then
      # Judge a link by where it points: a PATH entry left by an earlier install of ours resolves into the
      # cache or the interpreter's bin, and is not "somebody else's file" however it is spelled.
      local target; target="$(readlink "$destination")"
      case "$target" in /*) ;; *) target="$(dirname "$destination")/$target" ;; esac
      probe="$target"
    fi
    ours "$(normalize "$probe")"
  }
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    if reports_the_pin; then
      printf 'path: %s already reports %s; nothing linked\n' "$destination" "$expected_version"
    elif [ -d "$destination" ] && [ ! -L "$destination" ]; then
      if [ "$MIGRATE$FORCE" = "00" ]; then
        printf 'collision: %s is a directory this script does not manage; refusing to replace it.\n' "$destination" >&2
        printf '         --migrate-copies moves it aside to %s.pre-%s.bak first.\n' "$destination" "$REF" >&2
        exit 2
      fi
      backup="$destination.pre-$REF.bak"
      if [ -e "$backup" ]; then printf 'refusing to move %s aside: %s already exists\n' "$destination" "$backup" >&2; exit 1; fi
      mv "$destination" "$backup"; printf 'moved aside %s -> %s\n' "$destination" "$backup"
    elif destination_is_ours; then
      # This script's own artifact (pip's entrypoint, or the uv shim) at the wrong version: the install
      # above did not move it.  Deleting it here would leave no CLI at all behind a failed install, so it
      # stays and the attestation below fails naming both versions.
      here_version="$( "$destination" --version 2>/dev/null | head -n 1 | tr -d '[:space:]' || printf '<no version>')"
      printf 'path: %s is where this script installs, but reports %s instead of %s\n' \
        "$destination" "$here_version" "$expected_version" >&2
    else
      # A hand-written file, or a link into a tree this script did not install - a conda shim, a wrapper
      # that exports a token, an install from a vendor checkout.  Moving one aside is the operator's call.
      if [ "$MIGRATE$FORCE" = "00" ]; then
        printf 'collision: %s exists (%s) and does not report %s; refusing to replace it.\n' \
          "$destination" "$([ -L "$destination" ] && printf 'link to %s' "$(readlink "$destination")" || printf 'file')"\
          "$expected_version" >&2
        printf '         Either install herdr-handoff into %s yourself, or run\n' "$LINK_DIR" >&2
        printf '         scripts/install-handoff.sh --migrate-copies to back it up as %s.pre-%s.bak.\n' \
          "$destination" "$REF" >&2
        exit 2
      fi
      backup="$destination.pre-$REF.bak"
      if [ -e "$backup" ]; then printf 'refusing to move %s aside: %s already exists\n' "$destination" "$backup" >&2; exit 1; fi
      mv "$destination" "$backup"; printf 'moved aside %s -> %s\n' "$destination" "$backup"
    fi
  fi
  if [ ! -e "$destination" ] && [ ! -L "$destination" ]; then
    [ -n "$chosen" ] || { printf 'no entrypoint to link into %s (%s is not installed there)\n' "$LINK_DIR" "$PYTHON" >&2; exit 1; }
    mkdir -p "$LINK_DIR"
    # -n so a destination that is a symlink to a directory is replaced, not followed: without it the new
    # link was written *inside* the pinned checkout and the PATH entry never changed.
    ln -sfn "$chosen" "$destination"
    printf 'path: %s -> %s\n' "$destination" "$chosen"
  fi
  # Attestation.  An older CLI answering --version from PATH is the exact failure this step exists to
  # catch, and a link alone does not disprove it.  What is proven here is only the version the entrypoint
  # reports — the revision of the code behind it is never read, so a CLI built from an older commit that
  # still declares this version passes this check.  The line below says exactly that much; closing the gap
  # itself (a provenance sidecar written at install time and required before the word "attested") is
  # issue #802.
  [ -x "$destination" ] || { printf '%s is still not executable\n' "$destination" >&2; exit 1; }
  got_version="$("$destination" --version 2>/dev/null | head -n 1 | tr -d '[:space:]' || true)"
  if [ "$got_version" != "$expected_version" ]; then
    printf 'MISMATCH: herdr-handoff at %s reports %s; the pinned tree %s declares version %s\n' \
      "$destination" "${got_version:-<no version>}" "$REF" "$expected_version" >&2
    printf 'The CLI on PATH is not the revision this install fetched.  What --version reports wins over\n' >&2
    printf 'the symlink, the directory name and the pip log; remove or replace it, or run --migrate-copies.\n' >&2
    exit 1
  fi
  printf 'attested: %s reports version %s, which the pinned tree %s declares\n' "$destination" "$got_version" "$REF"
fi
