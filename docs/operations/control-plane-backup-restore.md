# Control-plane backup and restore

The main control-plane database (container `herdr-cp-postgres`, database `herdr`, volume `herdr-cp-pg`) holds the event log,
missions, sessions, token usage, scheduler state and the artifact registry. The control plane rebuilds its memory from it at
start, so losing it loses all of that. The object store (`herdr-cp-rustfs`, volume `herdr-cp-rustfs`, path `/data`) holds the
artifact payloads the registry points at. A database dump without its objects is a partial backup, so both are copied.

Run everything below on the control-plane host as the deploying user. No step prints a password: `pg_dump`, `pg_restore` and
`psql` run inside the database container over its local socket as the container's own `$POSTGRES_USER`.

Steps marked **untested** have not been run against a real control-plane host; see [What is untested](#what-is-untested).

## What is backed up, where, and when

| what | files | made by | kept |
|---|---|---|---|
| database, daily | `controlplane-<UTC>.dump` | `herdr-controlplane-backup.timer` (daily, `Persistent=true`, 15 min jitter) | newest 14 |
| object store, daily | `controlplane-objects-<UTC>.tar` | the same run | newest 14 |
| database, before a migration | `predeploy-controlplane-<UTC>.dump` | every `deploy.sh` that has a previous release, before the checkpoint prewarm applies migrations; a failed dump stops the deploy | newest 5 |

All files are in `~/.local/share/herdr-engineering/backups/control-plane/` (directory 0700, files 0600). Dumps are
`pg_dump -Fc` of the whole database, checked with `pg_restore --list`, written to a `.partial-*` name and renamed. They stay
on this host: copying them elsewhere is manual. The object copy is taken from the running store with `docker cp`, so it is
consistent per object but not a point-in-time snapshot.

The timer is installed by every real `deploy.sh`, or by hand with
`deploy/control-plane/memory-backup.sh install --target control-plane`. Take one now with
`~/.local/share/herdr-engineering/memory-backup.sh backup --target control-plane`. `herdr-eng doctor` reports the check
`control_plane_backup`: warn above 36 h, fail above 7 days, warn when no dump exists or the object archive is missing or stale.

## Restore the database

Restore into a new database, check it, then swap it in. The damaged database stays under a new name until you drop it.

1. **Pick the dump** and check it. Use the newest `controlplane-*.dump`, or the `predeploy-controlplane-*.dump` taken just
   before a bad migration.

   ```bash
   B=~/.local/share/herdr-engineering/backups/control-plane
   DUMP="$B/$(ls -1 "$B" | grep '^controlplane-2.*\.dump$' | sort | tail -n 1)"
   docker exec -i herdr-cp-postgres pg_restore --list < "$DUMP" | head
   ```

2. **Stop the writers.** Only `herdr-cp-web` and `herdr-cp-worker` connect to the database. Stop the worker first.

   ```bash
   docker stop -t 60 herdr-cp-worker herdr-cp-web
   ```

3. **Optionally keep a dump of what is there now**, if the database still answers:
   `memory-backup.sh backup --target control-plane --prefix before-restore --no-objects`.

4. **Restore into `herdr_restore`.** `--exit-on-error` makes any problem a failure; do not swap on a failure.

   ```bash
   docker exec herdr-cp-postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -qc "CREATE DATABASE herdr_restore"'
   docker exec -i herdr-cp-postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d herdr_restore --no-owner --exit-on-error' < "$DUMP"
   ```

5. **Check it before the swap.** Both numbers must be non-zero and the newest migration must be the one you expect.

   ```bash
   docker exec -i herdr-cp-postgres sh -c 'psql -U "$POSTGRES_USER" -d herdr_restore -At' <<'SQL'
   select count(*) from information_schema.tables where table_schema = 'public';
   select count(*), max(version) from schema_migrations;
   SQL
   ```

6. **Swap.** Renaming needs no other session on `herdr`; the terminate call clears stragglers.

   ```bash
   docker exec -i herdr-cp-postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1' <<'SQL'
   select pg_terminate_backend(pid) from pg_stat_activity where datname = 'herdr';
   ALTER DATABASE herdr RENAME TO herdr_damaged;
   ALTER DATABASE herdr_restore RENAME TO herdr;
   SQL
   ```

   To undo, rename the two back (stop the services again first).

7. **Restore the object store** only if it was lost too (stop `herdr-cp-rustfs` first, start it after). The archive holds a
   top-level `data/` directory; this puts it back over the stopped container's `/data` and does not remove files that are
   already there.

   ```bash
   docker stop herdr-cp-rustfs
   docker cp - herdr-cp-rustfs:/ < "$B/$(ls -1 "$B" | grep '^controlplane-objects-.*\.tar$' | sort | tail -n 1)"
   docker start herdr-cp-rustfs
   ```

8. **Start in dependency order**, waiting for each:

   ```bash
   docker start herdr-cp-postgres herdr-cp-rustfs        # already running if you did not stop them
   docker start herdr-cp-web
   curl -fsS "${HERDR_CP_HEALTH_URL:?the control plane health URL}"     # wait for "status":"ok"
   docker start herdr-cp-worker
   docker restart herdr-cp-dashboard-proxy herdr-llm-proxy              # the containers that depend on the web service, if present
   ```

   The web service and the worker run migrations when they start. A dump from an older release is migrated forward; a dump
   newer than the running image is not understood. Restoring a `predeploy-controlplane-*.dump` to leave a bad release means
   deploying the previous release too (`deploy.sh --ref <previous commit>`), since a rollback returns the image, not the schema.

9. **Verify.**
   - `docker ps --filter name=herdr-cp --format '{{.Names}} {{.Status}}'`: every container up, none restarting.
   - `docker logs --since 5m herdr-cp-worker` has no migration or connection errors.
   - `herdr-eng doctor`: `control_plane_backup` is not `fail`.
   - Open the dashboard and check that the newest events, sessions and the artifact list are the ones you expect from the
     dump's date; an artifact whose payload fails to load means step 7 is needed.
   - Once satisfied, `docker exec herdr-cp-postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE herdr_damaged"'`.

## Check a dump without touching the live database

`memory-backup.sh restore-test --target control-plane` restores the newest dump into a temporary database inside
`herdr-cp-postgres`, counts tables, and drops only that database. It adds load to the live Postgres while it runs, so run it
at a quiet time. Do this after the first real backup, and again after any change to the backup script.

## What is untested

Nothing here has been run against a real control-plane host or its real database. What exists:

- Tests of `memory-backup.sh`, `deploy.sh` and the doctor check run against a scripted `docker`, which proves the commands,
  the file handling, the retention and the failure paths, not that Postgres accepts them.
- The migrator lock is tested against a real Postgres 17.
- **Untested**: a first run of the backup timer, `pg_dump` of the real `herdr` database (size, duration, the 60 minute timer
  limit), the `docker cp` copy of the real object store (size, behaviour while it is being written), every restore step above
  including the rename swap and the object restore, and `restore-test --target control-plane`.
- **Not done**: copying dumps and archives off this host, and a restore rehearsal on a scratch volume. Until both exist, the
  backups protect against a bad migration or a dropped database, not against losing the host.
