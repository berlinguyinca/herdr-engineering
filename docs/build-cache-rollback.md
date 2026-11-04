# Fabric build cache: rollback

Rollback is a configuration change. No code revert, no data migration. It is
rehearsed by `tests/test_build_chaos.py::test_rollback_rehearsal_returns_to_legacy_exec_path`
(every feature enabled, then disabled or the section removed).

## 1. Config rollback (do this first)

Edit the host config (`~/.config/herdr-engineering/config.yaml`, or the file named
by `$HERDR_ENGINEERING_CONFIG`) in any ONE of these ways:

1. Set `fabric_build_cache.enabled: false`.
2. Remove the `fabric_build_cache:` section.
3. Leave a broken or half-edited section: it fails closed to the disabled state.

Effect, per the rehearsal test:

- `herdr-eng build-coordinate -- <cmd>` takes the original gate path: same exit
  code, no cache env (`RUSTC_WRAPPER`, `SCCACHE_*`), no `builds.jsonl`, no
  registry db, no `sccache/` directory created.
- `herdr-eng build-coordinate --status` and `--status --json` still work; the
  `slot_suggestion` field disappears.
- `--require-cache-enabled` (used by the Pi extension) execs the original command:
  no gate directory, no events.
- Existing cleanup keeps its legacy behaviour. Eviction stays dry-run unless
  `eviction.destructive_enabled` is true; with the feature off, nothing applies it.
- Builds already running keep their locks until they exit. Their leases expire by
  TTL and `reconcile` marks an attempt `owner_vanished` only when the lease is
  expired AND the owner pid is provably dead. Nothing is killed by rollback.

Disable shared storage alone with `shared_cache.enabled: false`; builds then use
the local cache only.

## 2. State files that may remain (OPERATOR ACTION, listing only)

Nothing below is deleted by rollback, by any herdr-eng command, or by the tests.
They are inert once the feature is off. Remove them only if you want the disk back,
and only after confirming no build is running on the host (`--status` shows no busy
slot). Paths are under the host state dir (`$HERDR_ENGINEERING_STATE`, default
`~/.local/state/herdr-engineering`), inside `build-gates/`:

| Path | What it is | Needed by legacy path |
|---|---|---|
| `builds.jsonl` | recorded build events | no |
| `build-registry.db`, `-wal`, `-shm` | sqlite build/lease registry | no (safe to ignore) |
| `sccache/` | local compile cache | no |
| `sccache/health-down-*` | shared-endpoint negative-cache markers | no |
| `gate.N`, `build-maintenance.lock` | slot and maintenance flock files | YES: keep |

Control plane: if `control_plane.build_cache.enabled` was on, turn it off too; the
`/api/v1/build-cache/*` routes then answer 404 and the capability `build_cache_v1`
disappears.

## 3. Pi extension ordering

1. First disable `fabric_build_cache` in config (section 1). The extension's
   `--require-cache-enabled` flag then makes every wrapped command a plain exec.
2. Only then, if needed, remove or revert the Pi build-gate extension
   (`deploy/pi-build-gate/build-gate.ts`). The old extension keeps gating heavy
   commands exactly as before.
3. Never remove the extension while the feature is still enabled: agents would
   then run un-traced builds against live leases.

## 4. The registry db is safe to ignore

With the feature off, nothing reads or writes `build-registry.db`. Cleanup consults
it only on the enabled path; if it is missing the legacy cleanup logic applies, and
if it is present but unreadable cleanup fails closed (`registry_unreadable`) and
deletes nothing. Test: `test_corrupt_registry_db_fails_cleanup_closed_but_not_builds`
and the last block of the rollback rehearsal.

## 5. Fail-safe behaviour worth knowing

A read-only or unwritable state dir no longer blocks a build: `build-coordinate`
prints `gate unavailable (state_dir_unwritable: ...); running ungated` and runs the
plain command with its exit code (fix commit dd331451).
