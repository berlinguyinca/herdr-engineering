#!/usr/bin/env bash
# Sourced by deploy.sh. Metadata is diagnostic only; flock is the sole authority.
# Keep the inode forever, and close (never LOCK_UN) adopted outer-wrapper FDs.
DEPLOY_LOCK_FD=""
DEPLOY_LOCK_OWNER=""

deploy_lock_acquire() {
  local candidate fd old_umask
  old_umask="$(umask)"; umask 077
  mkdir -p "$STATE_DIR" || return 1
  chmod 700 "$STATE_DIR" || return 1
  # Append opens without replacing/truncating the inode, even with a live wrapper.
  : >> "$STATE_DIR/rollout.lock"
  chmod 600 "$STATE_DIR/rollout.lock"
  umask "$old_umask"
  for candidate in /proc/"$$"/fd/*; do
    fd="${candidate##*/}"
    if ! [[ "$fd" =~ ^[0-9]+$ ]] || [ "$fd" -lt 3 ]; then continue; fi
    if [ "$candidate" -ef "$STATE_DIR/rollout.lock" ] && flock -n "$fd" 2>/dev/null; then
      DEPLOY_LOCK_FD="$fd"
      break
    fi
  done
  if [ -z "$DEPLOY_LOCK_FD" ]; then
    exec {DEPLOY_LOCK_FD}>>"$STATE_DIR/rollout.lock"
    if ! flock -n "$DEPLOY_LOCK_FD"; then
      python3 "$STATE_HELPER" lock-report "$STATE_DIR" >&2
      return 75
    fi
  fi
  DEPLOY_LOCK_OWNER="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  python3 "$STATE_HELPER" lock-start "$STATE_DIR" "$DEPLOY_LOCK_OWNER" "$$"
}

deploy_lock_phase() {
  [ -n "$DEPLOY_LOCK_OWNER" ] || return 0
  python3 "$STATE_HELPER" lock-phase "$STATE_DIR" "$DEPLOY_LOCK_OWNER" "$1"
}

deploy_lock_cleanup() {
  if [ -n "$DEPLOY_LOCK_OWNER" ]; then
    python3 "$STATE_HELPER" lock-clear "$STATE_DIR" "$DEPLOY_LOCK_OWNER" || true
  fi
  # Let process exit close descriptors, after *all* worktree/temp cleanup. An
  # inherited wrapper may still own the same open-file description afterwards.
}
