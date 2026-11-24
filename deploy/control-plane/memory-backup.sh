#!/usr/bin/env bash
# Back up (and restore-test) a Postgres database of the control-plane project: the Hindsight memory database (default)
# or, with `--target control-plane`, the main control-plane database and its object store.
#
#   memory-backup.sh backup        [--target T] [--dest DIR] [--container NAME] [--keep N] [--prefix P] [--no-objects]
#   memory-backup.sh restore-test  [--target T] [--dest DIR] [--container NAME] [--prefix P]
#   memory-backup.sh install       [--target T]   # stable script copy + the two user units, enable the timer
#   memory-backup.sh age           [--target T] [--dest DIR] [--prefix P]   # seconds since the newest dump (exit 1: none)
#
# Targets (each is only a set of defaults; every value below can still be overridden by a flag):
#   memory (default)  container herdr-hindsight-db, database/user hindsight, files memory-<stamp>.dump in
#                     ~/.local/share/herdr-memory/backups, units herdr-hindsight-backup.{service,timer}.
#   control-plane     container herdr-cp-postgres, database herdr, user read from the container's own POSTGRES_USER,
#                     files controlplane-<stamp>.dump in ~/.local/share/herdr-engineering/backups/control-plane, units
#                     herdr-controlplane-backup.{service,timer}. `backup` ALSO copies the object store (container
#                     herdr-cp-rustfs, path /data) with `docker cp` into controlplane-objects-<stamp>.tar, verified with
#                     `tar -t`; --no-objects skips that (deploy.sh takes a database-only dump before a migration).
#                     The object copy is taken from a running store, so it is crash-consistent per object, not a
#                     point-in-time snapshot; a failed object copy fails the run but leaves the good database dump.
#
# Same algorithm as the herdr-memory candidate's `herdr-memory-backup` (deploy/systemd there): `pg_dump -Fc` of the
# WHOLE database (every tenant schema, not just public) inside the database container into an owner-only partial file,
# `pg_restore --list` verifies the archive, the verified dump is renamed to <prefix>-<UTC stamp>.dump, and its sha256 is
# reported as one JSON line. It never touches the live database. `--keep N` (default 14) prunes only older
# <prefix>-*.dump (and <prefix>-objects-*.tar) files in DEST after a verified new one exists; `--keep 0` keeps everything.
# --prefix names a separate series (deploy.sh uses predeploy-controlplane so its dumps are pruned apart from the daily ones).
#
# restore-test restores the newest dump into a uniquely named TEMPORARY database inside the same container, counts the
# restored tables, and drops only that temporary database. The live database is never written.
#
# install puts a stable copy of this script in ~/.local/share/herdr-engineering/ and the target's user units (daily,
# Persistent; the files beside this script) in ~/.config/systemd/user, then enables the timer. The units use the defaults above.
# Nothing here reads or prints a password: the container's own trust/peer auth is used through `docker exec`.
set -euo pipefail
umask 077

ACTION="${1:-}"; [ $# -gt 0 ] && shift
TARGET=memory; DEST=""; CONTAINER=""; KEEP=14; PREFIX=""; OBJECTS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:?--target needs a value}"; shift 2 ;;
    --dest) DEST="${2:?--dest needs a value}"; shift 2 ;;
    --container) CONTAINER="${2:?--container needs a value}"; shift 2 ;;
    --keep) KEEP="${2:?--keep needs a value}"; shift 2 ;;
    --prefix) PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
    --no-objects) OBJECTS=0; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$KEEP" =~ ^[0-9]+$ ]] || { echo "--keep needs a whole number" >&2; exit 2; }
case "$TARGET" in
  memory)        # the candidate's default directory, so both tools see the same files
    D_DEST="$HOME/.local/share/herdr-memory/backups"; D_CONTAINER=herdr-hindsight-db; D_PREFIX=memory
    DB_USER=hindsight; DB_NAME=hindsight; UNIT=herdr-hindsight-backup; OBJ_CONTAINER=""; OBJECTS=0 ;;
  control-plane)
    D_DEST="$HOME/.local/share/herdr-engineering/backups/control-plane"; D_CONTAINER=herdr-cp-postgres; D_PREFIX=controlplane
    DB_USER=""; DB_NAME=herdr; UNIT=herdr-controlplane-backup; OBJ_CONTAINER=herdr-cp-rustfs ;;
  *) echo "unknown target: $TARGET (memory | control-plane)" >&2; exit 2 ;;
esac
DEST="${DEST:-$D_DEST}"; CONTAINER="${CONTAINER:-$D_CONTAINER}"; PREFIX="${PREFIX:-$D_PREFIX}"
OBJ_PATH=/data

private_dest() {
  [ ! -L "$DEST" ] || { echo "backup directory cannot be a symlink" >&2; return 1; }
  mkdir -p "$DEST" && chmod 700 "$DEST"
  [ "$(stat -c %u "$DEST")" = "$(id -u)" ] || { echo "backup directory must belong to the current user" >&2; return 1; }
}

# the database user: fixed for memory; for the control plane the container's own POSTGRES_USER (no password involved)
resolve_user() {
  [ -n "$DB_USER" ] && return 0
  DB_USER="$(docker exec "$CONTAINER" printenv POSTGRES_USER 2>/dev/null || true)"
  [[ "$DB_USER" =~ ^[A-Za-z0-9_]+$ ]] || { DB_USER=""; echo '{"error":"cannot read the database user from the container; is it running?"}'; return 1; }
}

backup_objects() {
  local part final stamp sum
  part="$(mktemp "$DEST/.partial-XXXXXX")"
  if ! docker cp "$OBJ_CONTAINER:$OBJ_PATH" - > "$part" 2>/dev/null || ! tar -tf "$part" >/dev/null 2>&1 || [ ! -s "$part" ]; then
    rm -f "$part"
    echo '{"error":"Object store copy failed or the archive is unreadable; the database dump above is still valid"}'
    return 1
  fi
  sum="$(sha256sum "$part" | cut -d' ' -f1)"
  stamp="$(date -u +%Y%m%dT%H%M%S.%NZ)"
  final="$DEST/$PREFIX-objects-$stamp.tar"
  mv "$part" "$final"
  printf '{"path":"%s","bytes":%s,"sha256":"%s","format_verified":true}\n' "$final" "$(stat -c %s "$final")" "$sum"
  prune "$PREFIX-objects-*.tar"
}

# prune <glob>: only same-pattern files, and only after a new verified one exists
prune() {
  if [ "$KEEP" -gt 0 ]; then
    # shellcheck disable=SC2086,SC2012  # $1 is an intentional glob built from the validated prefix
    # shellcheck disable=SC2086,SC2012  # $1 is an intentional glob built from the validated prefix
    ls -1 "$DEST"/$1 2>/dev/null | sort | head -n "-$KEEP" | while IFS= read -r old; do rm -f -- "$old"; done
  fi
}

backup() {
  private_dest || return 1
  resolve_user || return 1
  local part final stamp sum
  part="$(mktemp "$DEST/.partial-XXXXXX")"
  if ! docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --format=custom --no-password > "$part" 2>/dev/null \
      || ! docker exec -i "$CONTAINER" pg_restore --list < "$part" >/dev/null 2>&1; then
    rm -f "$part"
    echo '{"error":"Database backup or archive verification failed; inspect database availability and destination permissions"}'
    return 1
  fi
  [ -s "$part" ] || { rm -f "$part"; echo '{"error":"Database backup is empty"}'; return 1; }
  sum="$(sha256sum "$part" | cut -d' ' -f1)"
  stamp="$(date -u +%Y%m%dT%H%M%S.%NZ)"
  final="$DEST/$PREFIX-$stamp.dump"
  mv "$part" "$final"
  printf '{"path":"%s","bytes":%s,"sha256":"%s","format_verified":true}\n' "$final" "$(stat -c %s "$final")" "$sum"
  prune "$PREFIX-*.dump"
  if [ "$OBJECTS" = 1 ]; then backup_objects; fi
}

restore_test() {
  local newest tmpdb n
  resolve_user || return 1
  newest="$(ls -1 "$DEST"/"$PREFIX"-*.dump 2>/dev/null | sort | tail -n 1 || true)"
  [ -n "$newest" ] || { echo '{"error":"no backup to test"}'; return 1; }
  tmpdb="restore_test_$(date -u +%Y%m%d%H%M%S)_$RANDOM"
  [[ "$tmpdb" =~ ^restore_test_[0-9]+_[0-9]+$ ]] || return 1
  drop_tmp() { docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -qc "DROP DATABASE IF EXISTS \"$tmpdb\"" >/dev/null 2>&1 || true; }
  docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -qc "CREATE DATABASE \"$tmpdb\"" >/dev/null || return 1
  # --no-owner: ownership noise is irrelevant for the check; errors are real failures
  docker exec -i "$CONTAINER" pg_restore -U "$DB_USER" -d "$tmpdb" --no-owner --exit-on-error < "$newest" >/dev/null 2>&1 \
    || { drop_tmp; echo '{"error":"restore into the temporary database failed"}'; return 1; }
  n="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$tmpdb" -Atc \
       "select count(*) from information_schema.tables where table_schema not in ('pg_catalog','information_schema')" || true)"
  drop_tmp
  [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -gt 0 ] || { echo '{"error":"restored database holds no tables"}'; return 1; }
  printf '{"restored":"%s","tables":%s,"temporary_database_dropped":true}\n' "$(basename "$newest")" "$n"
}

age() {
  local newest now mt
  newest="$(ls -1 "$DEST"/"$PREFIX"-*.dump 2>/dev/null | sort | tail -n 1 || true)"
  [ -n "$newest" ] || { echo "no backups in $DEST" >&2; return 1; }
  now="$(date +%s)"; mt="$(stat -c %Y "$newest")"
  echo $((now - mt))
}

install_units() {
  local here share="$HOME/.local/share/herdr-engineering" u="$HOME/.config/systemd/user"
  here="$(cd "$(dirname "$0")" && pwd)"
  mkdir -p "$share" "$u"
  install -m 0755 "$0" "$share/memory-backup.sh"     # stable copy: deploy.sh runs from a temporary worktree
  install -m 0644 "$here/$UNIT.service" "$u/$UNIT.service"
  install -m 0644 "$here/$UNIT.timer" "$u/$UNIT.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT.timer"
}

case "$ACTION" in
  backup) backup ;;
  restore-test) restore_test ;;
  install) install_units ;;
  age) age ;;
  *) sed -n '2,/^set /{/^#/p}' "$0" >&2; exit 2 ;;
esac
