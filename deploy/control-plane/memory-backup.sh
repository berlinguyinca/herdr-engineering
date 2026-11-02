#!/usr/bin/env bash
# Back up (and restore-test) the Hindsight memory database of the control-plane project.
#
#   memory-backup.sh backup        [--dest DIR] [--container NAME] [--keep N]
#   memory-backup.sh restore-test  [--dest DIR] [--container NAME]
#   memory-backup.sh install                 # stable script copy + the two user units, enable the timer
#   memory-backup.sh age           [--dest DIR]       # seconds since the newest backup (exit 1: none)
#
# Same algorithm as the herdr-memory candidate's `herdr-memory-backup` (deploy/systemd there): `pg_dump -Fc` of the
# WHOLE database (every tenant schema, not just public) inside the database container into an owner-only partial file,
# `pg_restore --list` verifies the archive, the verified dump is renamed to memory-<UTC stamp>.dump, and its sha256 is
# reported as one JSON line. It never touches the live database. `--keep N` (default 14) prunes only older
# memory-*.dump files in DEST after a verified new one exists; `--keep 0` keeps everything.
#
# restore-test restores the newest dump into a uniquely named TEMPORARY database inside the same container, counts the
# restored tables, and drops only that temporary database. The live `hindsight` database is never written.
#
# install puts a stable copy of this script in ~/.local/share/herdr-engineering/ and the user units
# herdr-hindsight-backup.{service,timer} (daily, Persistent; the files beside this script) in
# ~/.config/systemd/user, then enables the timer. The units use the defaults above.
# Nothing here reads or prints a password: the container's own trust/peer auth is used through `docker exec`.
set -euo pipefail
umask 077

ACTION="${1:-}"; [ $# -gt 0 ] && shift
DEST="$HOME/.local/share/herdr-memory/backups"     # the candidate's default, so both tools see the same files
CONTAINER="herdr-hindsight-db"
KEEP=14
while [ $# -gt 0 ]; do
  case "$1" in
    --dest) DEST="${2:?--dest needs a value}"; shift 2 ;;
    --container) CONTAINER="${2:?--container needs a value}"; shift 2 ;;
    --keep) KEEP="${2:?--keep needs a value}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$KEEP" =~ ^[0-9]+$ ]] || { echo "--keep needs a whole number" >&2; exit 2; }

private_dest() {
  [ ! -L "$DEST" ] || { echo "backup directory cannot be a symlink" >&2; return 1; }
  mkdir -p "$DEST" && chmod 700 "$DEST"
  [ "$(stat -c %u "$DEST")" = "$(id -u)" ] || { echo "backup directory must belong to the current user" >&2; return 1; }
}

backup() {
  private_dest || return 1
  local part final stamp sum
  part="$(mktemp "$DEST/.partial-XXXXXX")"
  if ! docker exec "$CONTAINER" pg_dump -U hindsight -d hindsight --format=custom --no-password > "$part" 2>/dev/null \
      || ! docker exec -i "$CONTAINER" pg_restore --list < "$part" >/dev/null 2>&1; then
    rm -f "$part"
    echo '{"error":"Database backup or archive verification failed; inspect database availability and destination permissions"}'
    return 1
  fi
  [ -s "$part" ] || { rm -f "$part"; echo '{"error":"Database backup is empty"}'; return 1; }
  sum="$(sha256sum "$part" | cut -d' ' -f1)"
  stamp="$(date -u +%Y%m%dT%H%M%S.%NZ)"
  final="$DEST/memory-$stamp.dump"
  mv "$part" "$final"
  printf '{"path":"%s","bytes":%s,"sha256":"%s","format_verified":true}\n' "$final" "$(stat -c %s "$final")" "$sum"
  if [ "$KEEP" -gt 0 ]; then   # only verified, same-pattern files, and only after a new one exists
    ls -1 "$DEST"/memory-*.dump 2>/dev/null | sort | head -n "-$KEEP" | while IFS= read -r old; do rm -f -- "$old"; done
  fi
}

restore_test() {
  local newest tmpdb n
  newest="$(ls -1 "$DEST"/memory-*.dump 2>/dev/null | sort | tail -n 1 || true)"
  [ -n "$newest" ] || { echo '{"error":"no backup to test"}'; return 1; }
  tmpdb="restore_test_$(date -u +%Y%m%d%H%M%S)_$RANDOM"
  [[ "$tmpdb" =~ ^restore_test_[0-9]+_[0-9]+$ ]] || return 1
  drop_tmp() { docker exec "$CONTAINER" psql -U hindsight -d postgres -qc "DROP DATABASE IF EXISTS \"$tmpdb\"" >/dev/null 2>&1 || true; }
  docker exec "$CONTAINER" psql -U hindsight -d postgres -qc "CREATE DATABASE \"$tmpdb\"" >/dev/null || return 1
  # --no-owner: ownership noise is irrelevant for the check; errors are real failures
  docker exec -i "$CONTAINER" pg_restore -U hindsight -d "$tmpdb" --no-owner --exit-on-error < "$newest" >/dev/null 2>&1 \
    || { drop_tmp; echo '{"error":"restore into the temporary database failed"}'; return 1; }
  n="$(docker exec "$CONTAINER" psql -U hindsight -d "$tmpdb" -Atc \
       "select count(*) from information_schema.tables where table_schema not in ('pg_catalog','information_schema')" || true)"
  drop_tmp
  [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -gt 0 ] || { echo '{"error":"restored database holds no tables"}'; return 1; }
  printf '{"restored":"%s","tables":%s,"temporary_database_dropped":true}\n' "$(basename "$newest")" "$n"
}

age() {
  local newest now mt
  newest="$(ls -1 "$DEST"/memory-*.dump 2>/dev/null | sort | tail -n 1 || true)"
  [ -n "$newest" ] || { echo "no backups in $DEST" >&2; return 1; }
  now="$(date +%s)"; mt="$(stat -c %Y "$newest")"
  echo $((now - mt))
}

install_units() {
  local here share="$HOME/.local/share/herdr-engineering" u="$HOME/.config/systemd/user"
  here="$(cd "$(dirname "$0")" && pwd)"
  mkdir -p "$share" "$u"
  install -m 0755 "$0" "$share/memory-backup.sh"     # stable copy: deploy.sh runs from a temporary worktree
  install -m 0644 "$here/herdr-hindsight-backup.service" "$u/herdr-hindsight-backup.service"
  install -m 0644 "$here/herdr-hindsight-backup.timer" "$u/herdr-hindsight-backup.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now herdr-hindsight-backup.timer
}

case "$ACTION" in
  backup) backup ;;
  restore-test) restore_test ;;
  install) install_units ;;
  age) age ;;
  *) sed -n '2,/^set /{/^#/p}' "$0" >&2; exit 2 ;;
esac
