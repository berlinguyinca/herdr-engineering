# Fabric build cache: staged rollout runbook (spec 14)

Status of this document: written from the code and tests in branch
`feat/fabric-build-cache`. **Nothing here has been run on a real host.** No sccache,
second host and no Slurm was available to the implementation. The shared tier has been
measured on one host against the real herdr rustfs (see `docs/build-cache-storage.md`,
"Measured on rustfs"); everything else below is a contract from the code and tests, not
an observation. There is no measured baseline saving; do not quote one until Stage 1
produces it (see `IMPLEMENTATION_REPORT.md`).

Rules that apply to every stage:

- The operator deploys. The implementation never merges, pushes, installs, restarts or
  contacts a host.
- Every stage is one config change. Go back with `docs/build-cache-rollback.md`.
- Canary is one host at a time. Do not advance a stage without the listed evidence.
- CLI names: the spec's `herdr-eng build-cache status` does not exist. The real
  commands are `herdr-eng build-coordinate --status [--json]`,
  `build-cache-inventory [--json]`, `build-cache-evict [--json]` (dry-run unless
  `--apply`), `build-cache-slurm-plan [--json]` and `build-cleanup --dry-run`.
- Config file: `~/.config/herdr-engineering/config.yaml` or `$HERDR_ENGINEERING_CONFIG`.
  The `fabric_build_cache` section is read ONLY from there, never from a
  `herdr-engineering.yaml` in a project directory. A broken section fails closed to
  disabled (`build-coordinate` and `build-cache-inventory` refuse it loudly).
  Needs PyYAML (the fallback parser cannot read lists).

## Stage 0: merge (operator)

Evidence before merging: rerun
`python3 -m pytest tests/test_build_*.py tests/test_config*.py tests/control_plane/test_build_cache_api.py tests/test_cleanup_deploy.py -q -p no:cacheprovider`
(522 passed, 7 skipped at commit 1f5bab30 on the implementation host), plus the
repo's CI. Not yet run: the Playwright ui-gates, the node unit suite in CI, a
TypeScript typecheck of `deploy/pi-build-gate/build-gate.ts`. Run or waive them
explicitly.

Merging deploys nothing and changes no behaviour: every new path is default-off.
Operator approves: merge and push; the deploy itself is theirs afterwards.

No-go: CI red, or the ui-gates failing on `build-cache-view.js`.

## Stage 1: Bender local pilot

sccache 0.19.0 is now installed by the installer (operator-approved 2026-11-04; done on
bender). The code requires >= 0.19.0 (`MIN_SCCACHE_VERSION`). Installing it changes
nothing until `fabric_build_cache.enabled` is set.

Also choose the pilot project (a Rust workspace that builds offline or whose
dependencies are already fetched). Do the benchmark in the throwaway copy that
`--copy` creates, never in a live worktree.

Install (per-user, no root; SHA-256 pinned in `scripts/sccache-checksums.txt`; also
runs as step 2b of a full `scripts/install.sh`; `--no-build-cache-tools` skips it), then verify:

```bash
scripts/install.sh --build-cache-tools-only         # -> ~/.local/bin/sccache
herdr-eng doctor                                    # build_cache_sccache / _rust / _python_deps
sccache --version                                   # expect 0.19.0 or newer
rustc -vV && cargo --version
```

sccache does not cache incremental builds or binary crates: use `CARGO_INCREMENTAL=0`
and measure libraries.

Config (everything else stays default-off):

```yaml
fabric_build_cache:
  enabled: true
  mode: local_only
  allowed_hosts: [bender]
  local:
    capacity_gib: 20
    min_free_disk_percent: 20
    incremental_disabled_for_cacheable_builds: true
  cleanup:
    dry_run: true
```

Shared, Slurm, eviction (`eviction.destructive_enabled`), `adaptive_slots`, and
`control_plane.build_cache` all stay off.

Verify wiring (read-only):

```bash
herdr-eng build-cache-inventory --json              # sccache present, version, compat_key
herdr-eng build-coordinate --status --json
herdr-eng build-cleanup --dry-run                   # still dry-run
```

Measure. The benchmark runs the given command directly, not through the gate, so
the cached run must name `build-coordinate` as the command. Whether the bench's
sccache delta is read correctly through that wrapper is UNVERIFIED; also read
`builds.jsonl` (`<state_dir>/build-gates/builds.jsonl`) and `sccache --show-stats`.

```bash
# A. baseline, feature OFF (set fabric_build_cache.enabled: false first)
python3 scripts/build_bench.py --repeat 3 --dir <PROJECT> --cold --copy -- cargo build --offline > baseline.json
# B. feature ON: cold then warm through the gate
python3 scripts/build_bench.py --repeat 3 --dir <PROJECT> --cold --copy -- \
    herdr-eng build-coordinate --record -- cargo build --offline > cached-cold.json
# warm: second pass without --cold (reuses sccache entries; target/ is not cleaned)
python3 scripts/build_bench.py --repeat 3 --dir <PROJECT> --copy -- \
    herdr-eng build-coordinate --record -- cargo build --offline > cached-warm.json
sccache --show-stats
tail -n 4 ~/.local/state/herdr-engineering/build-gates/builds.jsonl
```

Record ONLY what the files show: elapsed seconds, `sccache_stats` hits and misses,
`proc_io_write_bytes` (a logical lower bound, per process) and
`host_disk_write_bytes_delta` (device-wide, includes other activity; label it so).
Fields reported `unavailable` stay blank. Compare the built artifacts from A and B
(for example sha256 of the binary, and the `cargo test` result).

Go evidence: sccache version >= 0.19.0; last build event has `cache.applied: true`;
warm run shows hits in sccache stats; outputs match baseline; baseline and cached
numbers are both on file and attached to the report; no busy-slot or cleanup
regression (`build-coordinate --status` clean afterwards).
No-go: outputs differ, applied is false with a reason you cannot fix, or the warm run
shows no hits. Note crates of type bin, proc-macro and cdylib are not cacheable
upstream, so a partial hit rate is expected; do not call it a failure without data.

Operator approves explicitly: sccache install, which host, which project, merge/push
already done in Stage 0. Rollback: Stage 1 section below.

## Stage 2: shared canary on rustfs (read-only first)

The only shared backend is the herdr rustfs server (`herdr-cp-rustfs`, a MinIO drop-in).
No AWS or other S3 service. The operator provisions it once, then enables one host:

1. `herdr-eng build-cache-rustfs-provision` (dry-run), review, then `--apply`. This creates
   bucket `herdr-build-cache` (hard quota 50 GiB by default), users `herdr-build-cache-rw`
   and `herdr-build-cache-ro`, and the 0600 files `build-cache-rustfs.cred`,
   `build-cache-rustfs-ro.cred`, `build-cache-rustfs-registry.yaml` in
   `~/.config/herdr-engineering/`. It does not touch `herdr-artifacts` and deletes nothing.
   Done on bender on 2026-11-04.
2. Config key names keep their S3 spelling for compatibility (`endpoint_ref`,
   `registry_file`, `credentials_file`); they refer to rustfs's S3 API.
3. Plain HTTP to loopback or tailnet hosts is accepted by validation; any other host needs TLS.

Canary config (read-only, one host):

```yaml
fabric_build_cache:
  enabled: true
  mode: shared_cache
  allowed_hosts: [bender]
  shared_cache:
    enabled: true
    endpoint_ref: build-cache
    namespace: trusted-dev
    writes_enabled: false
    tls_required: true
    credentials_file: ~/.config/herdr-engineering/build-cache-rustfs-ro.cred   # absolute path in practice
    registry_file: ~/.config/herdr-engineering/build-cache-rustfs-registry.yaml
```

(Use absolute paths.) For the writer host use the `-rw` credentials file with
`writes_enabled: true`, as a second, separately approved step.

How it works and what is verified: `build-coordinate` starts or reuses a per-policy sccache
server before cargo with the credentials in the SERVER environment only; the tier is
reported `active` only after the server is verified (bucket and prefix from
`--show-stats`, credentials from a signed list request). See
`docs/build-cache-storage.md` for the measured cold/warm/other-namespace/read-only runs.

Checks: `herdr-eng build-cache-inventory --json` reports the shared policy (configured
intent); the build event `cache.shared` shows `{active, reason}` (truth); a different
namespace must miss; unreachable rustfs leaves the build correct with
`shared_unavailable:endpoint_unreachable` and the same exit code (tested against
`100.64.0.1:9` with real sccache and cargo).

Known blocker for other hosts: the tailnet address `100.104.39.6:9000` currently rejects
valid SigV4 requests (it is the fabric agent's forwarder, rustfs is published on loopback
only). The operator must publish rustfs on the tailnet address or fix the forwarder before
a second host can use the shared tier.

Go evidence: shared hit on a build with an empty local cache (measured on one host);
read-only credentials unable to write (measured); outputs match baseline; outage drill
leaves builds correct. The bucket quota is enforced by rustfs (50 GiB hard);
`max_total_gib` in the config is still not enforced by herdr.
No-go: any credential appearing in env of cargo, events, logs or API output; a
wrong-namespace hit; read-only credentials able to write.

Limits to accept knowingly: sccache does not authenticate cached objects (write access
means cache poisoning), and same-user code can read the server's environment and the
credentials file. Use the RO user for anything untrusted.

Enable writes for Bender (spec phase 3) only as a second, separately approved step.

## Stage 3: second host

Needs an operator-chosen second host with a matching OS/arch and toolchain, and
verified host identity, performed by the operator (not available to the implementation;
the "second cache dir" run in the storage doc is a same-host simulation only). It also
needs the tailnet endpoint to work (Stage 2 blocker). Install sccache 0.19.0
there (approval again), run `build-cache-inventory --json` on both hosts and compare
`compat_key`: equal keys mean addressable objects, a mismatch lists the reason.
Reuse proof (a build on host B hitting entries written by host A) is the exit
criterion and has not been attempted. Cross-platform: the namespace includes OS and
arch, so different platforms never share objects.

Canary rule: at most one new host per step.

## Stage 4: Slurm (separate operator approval)

Not deliverable now. `herdr_engineering/build_slurm_adapter.py` is a non-executing
contract: only `NullRunner` exists, it always answers `runner_unavailable`, and
`build-cache-slurm-plan` prints "not submitted; no runner is verified". No Slurm
binary is invoked anywhere. A real runner, an approved CPU test partition, an account
and a UC Davis HPC review (load the `ucd-hpc` skill; no login-node compute) are all
operator decisions and must be approved separately. Two switches overlap:
`fabric_build_cache.slurm.*` and `execution.slurm_enabled/allowed_partitions`; the
adapter needs the master switch plus `slurm.enabled` and uses
`slurm.allowed_partitions`. Settle which is authoritative before enabling.
sccache-dist is NO-GO (`docs/build-cache-dist-evaluation.md`).

## Control plane flag

Off by default. The dashboard panel and `/api/v1/build-cache/*` need all of:

```yaml
control_plane:
  build_cache:
    enabled: true
    # state_dir and fabric_build_cache as documented in build_cache_routes.py
```

Effects: routes answer 404 when off; capability `build_cache_v1` appears only when
on; an enabled-but-invalid `fabric_build_cache` section stops control-plane start.
Data is this host's own `builds.jsonl` plus anything other hosts push when `control_plane.build_cache.ingest.enabled` (see "Report this host's builds" below). HTTP
cleanup execute answers 501 by design; eviction apply is CLI-only. Roll out after
Stage 1 has recorded builds to show. Rollback: set it false.

## Report this host's builds to the control plane (opt-in)

Off by default. A host pushes the non-secret summary of its recent builds (never argv, env, cwd or paths) to
`POST /api/v1/host/build-cache/report`. The control plane must have `control_plane.build_cache.ingest.enabled: true`
and an ingest-role key bound to this host's id.

1. Put the key in a file only you can read: `install -m 600 /dev/null ~/.config/herdr-engineering/build-cache-ingest.key`,
   then write the key into it (not onto a command line). A file readable by group or others is refused (`chmod 600`).
2. In `~/.config/herdr-engineering/config.yaml` (YAML only; there is no environment variable and no key option on argv):

```yaml
fabric_build_cache:
  report:
    enabled: true
    url: https://<control-plane host>            # https required; http only for 127.0.0.1/localhost
    key_file: /home/<you>/.config/herdr-engineering/build-cache-ingest.key
    interval_s: 60                                # 10..3600
    host_id: <optional; defaults to the hostname and must match the key's host binding>
```

3. Try once: `herdr-eng build-cache-report --once --json` (exit 0 sent, 1 transport or server refusal, 2 refused to run:
   disabled, non-https url, missing/empty/loose key file, unknown key).
4. Schedule it, not enabled by default:
   - Linux: install `scripts/herdr-eng-build-cache-report.service` as a systemd user unit (steps are in the file header),
     then `systemctl --user enable --now herdr-eng-build-cache-report.service`.
   - macOS: render `scripts/com.herdr-engineering.build-cache-report.plist` into `~/Library/LaunchAgents/` and
     `launchctl bootstrap gui/$(id -u) ...` (steps are in the file header).

`--loop` logs one `build-cache-report:` line per attempt on stderr and survives an unreachable or refusing control
plane; the key file is re-read each attempt, so rotating the key needs no restart. Rollback: disable the unit
(`systemctl --user disable --now ...` / `launchctl bootout ...`) or set `report.enabled: false`.

## Pi extension deploy ORDER

1. Deploy `herdr-eng` (this branch) to the host first.
2. Then deploy `deploy/pi-build-gate/build-gate.ts` (install.sh / fleet sync).

An old `herdr-eng` rejects the new flags (`--require-cache-enabled`, trace flags) with
an argparse error and the build fails. With `fabric_build_cache` off the extension
passes `--require-cache-enabled` and herdr-eng execs the original command unchanged.
Reverse order on rollback: disable the config, then revert the extension.

## Per-stage rollback

Authoritative procedure: `docs/build-cache-rollback.md`.

- Stage 1: `fabric_build_cache.enabled: false` (or delete the section). Legacy gate
  path returns; `sccache/`, `builds.jsonl`, `build-registry.db` remain inert; do not
  delete them without confirming no build is running.
- Stage 2: `shared_cache.enabled: false` first (local cache continues), then the
  whole section if needed. Never delete the central cache without approval.
- Stage 3: same on the second host; independent per host.
- Stage 4: nothing runs; `slurm.enabled: false`.
- Control plane: `control_plane.build_cache.enabled: false`.
- Pi extension: config first, extension second, never the reverse.
Note: that doc says `/api/build-cache/*`; the routes are under `/api/v1/build-cache/`.
