# Fabric build cache: staged rollout runbook (spec 14)

Status of this document: written from the code and tests in branch
`feat/fabric-build-cache`. **Nothing here has been run on a real host.** No sccache,
no S3 endpoint, no second host, no Slurm was available to the implementation. Every
"expected" outcome below is a contract from the code and tests, not an observation.
There is no measured baseline and no measured saving; do not quote one until Stage 1
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

## Stage 2: shared canary (read-only first)

Needs explicit operator decisions that the implementation will not make:

1. A TLS S3-compatible endpoint (hostname, region) and a bucket. The loopback MinIO
   in `deploy/control-plane/compose.yaml` is not a shared endpoint.
2. A scoped credentials file, mode 0600, owned by the service user, absolute path.
   Read-only credentials for the canary. Write credentials only at the later
   writes step, and separately approved.
3. A protected service registry YAML (mode 0600) mapping `endpoint_ref` to
   host/region/bucket (`shared_cache.registry_file`). `endpoint_ref` is an
   identifier, never a URL; no secret goes in the YAML or in GitHub.

**REMAINING IMPLEMENTATION GAP (read before approving Stage 2).** Today a fully valid
shared request does NOT reach the S3 level. After the security review (H2, H3) it
runs on the local cache only and reports `shared_unavailable:shared_requires_server`;
`shared.active` is never true, and no `AWS_*`, `SCCACHE_CONF` or
`SCCACHE_S3_RW_MODE` is put in cargo's environment (build scripts and proc macros
could read it). Delivering shared reads needs code that does not exist yet: an
explicit `sccache --start-server` per policy with the credentials file read into the
server child's environment only, plus server lifetime management. Until that is
built, reviewed and tested, Stage 2 cannot produce a shared hit and must not be
approved as a performance step. What can be checked now is only that configuration
validates and the degradation is reported correctly.

Config for when the gap is closed (read-only canary on one host):

```yaml
fabric_build_cache:
  enabled: true
  mode: shared_cache
  allowed_hosts: [bender]
  shared_cache:
    enabled: true
    endpoint_ref: <registry id chosen by the operator>
    namespace: trusted-dev
    writes_enabled: false
    tls_required: true
    credentials_file: /absolute/path/readonly-creds   # 0600
    registry_file: /absolute/path/registry.yaml       # 0600
```

Checks (read-only): `herdr-eng build-cache-inventory --json` reports the shared
policy; the build event `cache.shared` shows `{active, reason}`; a wrong namespace
must miss; stopping the endpoint must leave the build correct with
`shared_unavailable:*` and the same exit code (covered by chaos tests against
fake listeners only). Verify against the live endpoint, which no test has done:
read-only enforcement (`SCCACHE_S3_RW_MODE`), TOML `region`, `key_prefix`
normalisation, path-style addressing, behaviour on a refused read. Herdr-side
quarantine of corrupt entries is NOT implemented; sccache's own behaviour is
undocumented.

Go evidence: shared hit observed on a second build after a different build wrote the
entry (needs a writer, so only after writes are separately approved); read-only
credentials proven unable to write; outputs match baseline; outage drill leaves
builds correct. Bucket-side quota is the operator's: `max_total_gib` is not enforced
anywhere.
No-go: any credential appearing in env, events, logs or API output; a wrong-namespace
hit; read-only credentials able to write.

Enable writes for Bender (spec phase 3) only as a second, separately approved step.

## Stage 3: second host

Needs an operator-chosen second host with a matching OS/arch and toolchain, and
verified host identity. Not available to the implementation. Install sccache 0.19.0
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
Data is this host's own `builds.jsonl` only (no ingest from other hosts). HTTP
cleanup execute answers 501 by design; eviction apply is CLI-only. Roll out after
Stage 1 has recorded builds to show. Rollback: set it false.

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
