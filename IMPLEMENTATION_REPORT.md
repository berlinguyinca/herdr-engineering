# Implementation report: Herdr fabric build cache (specs 00-14)

Branch `feat/fabric-build-cache`, code revision 1f5bab30 (docs added after it).
Nothing was deployed, merged, pushed, installed or run on any remote host. Rollout
procedure: `docs/build-cache-deployment.md`. Rollback: `docs/build-cache-rollback.md`.

Status vocabulary: complete+tested / complete+tested, not deployed / partial /
blocked / awaiting approval. "Tested" means the repo's own tests with real
processes, sockets, sqlite and temp dirs; sh stand-ins replace sccache/rustc/cargo
in contract tests, so they prove env/argument wiring and failure handling, never savings.

## Test evidence (re-run for this report)

```
python3 -m pytest tests/test_build_*.py tests/test_config*.py \
  tests/control_plane/test_build_cache_api.py tests/test_cleanup_deploy.py -q -p no:cacheprovider
-> 522 passed, 7 skipped in 110.49s
```

The 7 skips are the explicit spec 13 scenarios that need sccache, S3, Slurm, root or
a second host. Other counts quoted from HANDOFF.md (not re-run here): Playwright
`build-cache.test.mjs` 13 passed; `packages/*.test.js` 297 passed; Pi gate node test
11 passed (needs Node >= 22.18); control-plane regression subset 895 passed 2 skipped
(spec 10 time). Ruff was clean on touched files per HANDOFF; a pre-existing I001 in
`cli.py` remains.

## Status per component

| Component | Status | Notes |
|---|---|---|
| A local sccache (specs 01-03, 06-08) | complete+tested, not deployed | env wiring, measurement, fingerprint, quota/eviction planning, Pi gate; never run against a real sccache |
| B shared S3 (spec 04) | partial | config, rendering, health probe, fallback tested; shared level not delivered to builds (`shared_requires_server`); no real S3 |
| C coordination/leases (spec 05) | complete+tested, not deployed | `reconcile` has no caller |
| D dashboard + metrics (specs 09, 10) | partial | API, SSE, panel done (local host data only); metrics export to Grafana not implemented |
| E Slurm (specs 11, 12) | blocked | adapter contract only (NullRunner); dist is NO-GO doc; any real runner awaiting approval |

## Status per spec

| Spec | Status | Evidence |
|---|---|---|
| 00 audit | complete+tested | `AUDIT.md`; baseline 63 passed |
| 01 measure | complete+tested, not deployed | `build_metrics.py`, `scripts/build_bench.py`; Bender baseline NOT run |
| 02 config | complete+tested, not deployed | default-off, fail-closed section |
| 03 local | partial | wiring tested with fakes; exit criterion (warm hits on Bender) not run, sccache not installed |
| 04 storage | partial | see B; canary exit criteria not run |
| 05 leases | complete+tested, not deployed | registry, fencing, cleanup guard |
| 06 fabric | complete+tested, not deployed | inventory for this host only; two-host proof blocked |
| 07 policy | complete+tested, not deployed | dry-run default, no timer invokes it |
| 08 pi | partial | python and node tests pass; TS typecheck not run; no real Pi run |
| 09 api | partial | local-host data only, no ingest, some events missing |
| 10 ui | complete+tested, not deployed | Playwright 13 passed; read-only |
| 11 slurm | blocked | contract only; nothing submits; awaiting approval for any runner/partition |
| 12 dist | blocked | evaluation doc, NO-GO |
| 13 chaos | partial | local scenarios signed off; 7 skipped; one real bug found and fixed (dd331451) |
| 14 rollout | complete (docs only) | this report, deployment runbook |

## Acceptance matrix (from spec 13 evidence in HANDOFF.md)

1 Baselines: BLOCKED. 2 Functional correctness: BLOCKED (needs sccache). 3 Concurrency:
PARTIAL (20-build storm signed off; "4 identical, duplicate work bounded" blocked).
4 Active builds vs cleanup: SIGNED OFF. 5 Invalid inputs: PARTIAL (corrupt sccache
object blocked). 6 Cache outage: PARTIAL (real S3 503/retries blocked). 7 Cross-platform:
BLOCKED. 8 Disk pressure: not re-signed (disk-full needs root). 9 Control plane restart:
PARTIAL. 10 Slurm offline: SIGNED OFF (no Slurm invoked). 11 Slurm CPU reservation:
BLOCKED. 12 GPU partition: BLOCKED (nothing launches). 13 Dashboard/API: tests exist
(specs 09/10) but not signed off by spec 13. 14 Metrics: not signed off. 15 Rollback:
SIGNED OFF. 16 Performance go/no-go: BLOCKED (no measurement).

## Baseline comparison

**No measured baseline and no measured savings exist.** No cold/warm run was made, no
sccache hit rate observed, no write reduction or elapsed-time change measured. The
spec 14 exit criterion ("proof of reduced real writes or elapsed build times") is NOT
met. Any such number must come from Stage 1 of the runbook; none is estimated here.

## sccache install and smoke (2026-11-04)

sccache 0.19.0 installed on bender: **yes, 2026-11-04**, via `scripts/install.sh
--build-cache-tools-only` (checksum-pinned, `~/.local/bin/sccache`, `--version` = 0.19.0;
`herdr-eng doctor` build_cache_* all ok). Real smoke (tmp lib crate, `CARGO_INCREMENTAL=0`,
tmp `SCCACHE_DIR`, `cargo clean` between builds): first build 1 executed / 0 hits / 1 miss;
second build 2 executed / 1 hit / 1 miss, hit rate 50.00 %. This proves local sccache
works; it is NOT the spec-14 baseline. Still not done: shared S3, real baseline.

## What was never run

Real S3 or any shared endpoint; two hosts; any
Slurm command or partition; the Playwright ui-gates for the panel in CI; Grafana
push; a real Pi session; `tsc` on the Pi extension.

Metrics export: truthfully, **none is implemented.** `observability.enabled` and
`export_existing_metrics_pipeline` are validated config booleans with no consumer; a
grep of `herdr_engineering/` and `scripts/` finds no Prometheus, remote_write or Grafana
code for build-cache data. Pushing to grafana.metabolomics.us is not done. The only
telemetry is `builds.jsonl`, the host-local API/SSE and the dashboard panel.

## Known gaps

- `Registry.reconcile` has no caller (no controller or startup hook).
- SSE has no `id:`/Last-Event-ID; no resume beyond the shared stream's last-100 replay.
- No ingest of other hosts' builds; API/panel show the control-plane host only; no migration 0046.
- Missing events: `cache_stats_updated`, `storage_degraded`, `lease_changed`, `cleanup_completed`.
- `shared_requires_server`: shared S3 is inert until a credentialed per-policy
  `sccache --start-server` and server lifetime management exist.
- `slurm.*` overlaps `execution.slurm_enabled/allowed_partitions`; authority unsettled.
- Dashboard table is cramped on mobile (tables scroll in focusable regions).
- ui-gates not run; typecheck of the Pi extension not run.
- M6: SIGKILL of the record-mode wrapper orphans the build (no PDEATHSIG); documented, mitigated by lease expiry and cleanup refusing.
- Also: fenced finish leaves rows non-terminal; eviction has no timer; S3 quota not enforced; Herdr-side corrupt-entry quarantine not implemented; (rollback doc route drift fixed).
- Upstream sccache claims (TOML `region`, `key_prefix`, path-style, read-only enforcement) are UNVERIFIED against a live binary.

## Security review summary

Review after spec 05 found and fixed: H1 cwd-relative config could enable the feature
(now machine-local only); H2 credentials in the cargo env (removed; shared degrades to
local); H3 shared sccache server port/policy collision (per-policy port); M1-M5
registry pid reuse, lease fencing after takeover, signal races, network-fs warning,
fail-closed config. Open: M6 and L2-L13 documented only. Design properties: no argv/env
in events, redaction on API/SSE, cleanup fails closed on an unreadable registry,
eviction dry-run default and requires `destructive_enabled` plus `--apply`, no delete
of symlinks or paths outside the cache, Slurm outputs validated, no secrets in the
generated sccache TOML.

### Adversarial review of specs 06-13 (done; fixes on this branch)

Findings: 0 critical, 2 high, 6 medium, 8 low. The server never deletes anything; no
path let `--apply` delete outside the cache dir or bypass `destructive_enabled`.

Fixed (each with a test, real deps, no mocks):
- H1 Pi extension/CLI version skew: the extension probes `herdr-eng build-coordinate --help`
  once at load; without `--require-cache-enabled` and `--trace-*` (or on probe failure) the
  light path is skipped and the legacy heavy rewrite is byte-identical. Tested with real stub
  executables on PATH for old and new help output.
- H2 sccache server pinning the slot flock / maintenance LOCK_SH: the wrapper now starts the
  per-policy server itself (`sccache --start-server`, `close_fds=True`, stdio detached) before
  the build, so the long-lived daemon never inherits lock fds. Tested with a real daemonizing
  stub that outlives the build (slot free, LOCK_EX obtainable, second build acquires). Design
  note: the build's direct child still inherits the lock fds on purpose (an orphaned build after
  wrapper SIGKILL keeps cleanup refused; covered by a chaos test). A grandchild daemon spawned by
  the build itself (not the sccache server) can still pin them.
- M1 unwritable-state-dir fallback now `exec`s (SIGINT reaches the command's own trap).
- M2 sccache port range 20000-31999 (below ephemeral 32768-60999), `SCCACHE_IGNORE_SERVER_IO_ERROR=1`.
  Collisions among policies/other listeners are still possible (12000 slots) and degrade to uncached.
- M3 cache is not applied when argv has `--config ...rustc-wrapper` or a cargo config
  (`.cargo/config*` up the tree, `$CARGO_HOME`) sets `build.rustc-wrapper`/workspace wrapper.
- M4 light commands wait at most 5 s (`HERDR_BUILD_GATE_LIGHT_WAIT`) for a slot, then run ungated.
- M5 Slurm adapter: build/attempt ids, epoch and digests validated and quoted before rendering
  (injection test); symlinked directories flagged anywhere in the output tree; staging hashes
  regular files with O_NOFOLLOW|O_NONBLOCK in a stream (FIFO no longer hangs); preview does not
  read the source tree unless slurm is requested; cancel with a malformed binding is refused.
- M6 `build_cache` event stream bounded (500 per host, trimmed at 2x).
- L4 trace ids use `sanitize_trace_id`; L6 `HERDR_ENG` shell-quoted, slots must be digits.
- Doc drift: `docs/build-cache-rollback.md` now cites `/api/v1/build-cache/*`.

Documented, not fixed (see HANDOFF.md "Review fixes (specs 06-13)"): L1 unlink TOCTOU, L2 atime
recheck, L3 two dry-run knobs, L5 hosts() stampede, L7 check order, L8 adaptive slots; the
`output reads follow a swapped symlink` window is narrowed (O_NOFOLLOW) but not closed for
parent directories.

Spec-05 HIGHs re-checked by the reviewer and CLOSED: H1 (repo-local config cannot select
policy), H2 (no credentials in cargo env; shared mode degrades to `shared_requires_server`),
H3 (per-policy port; server lock-fd inheritance and port range now also fixed).

Status caveat: spec 08 stays "partial" (python + node tests pass; TypeScript typecheck not
run, no real Pi run). Nothing here was run against a real sccache or Slurm.

## Safety-rule compliance checklist

| Rule | Evidence |
|---|---|
| Docs only for spec 14; no code, deploy, ssh, install, Slurm | only `docs/`, `IMPLEMENTATION_REPORT.md`, HANDOFF.md changed in the spec 14 commit |
| Everything default-off | `config/herdr-engineering.example.yaml`, DEFAULTS; `control_plane.build_cache.enabled` default off |
| Never delete without dry-run/consent | eviction/cleanup dry-run default; apply needs flag + `destructive_enabled`; rollback deletes nothing |
| No active-build deletion | lease + flock guards; chaos tests |
| No secrets in repo/env/events | credentials by 0600 file path; `test_*_secret_*` chaos checks; no ${ENV} secrets added |
| No Slurm/remote action without approval | `NullRunner`; no Slurm binary invoked |
| No GPU for CPU builds | sbatch render has no GPU; `gpu_for_cpu_builds` only warns |
| No invented numbers | baseline section above; all UNVERIFIED items marked |
| User deploys themselves | runbook; nothing deployed |
| Memory/telemetry host claims | none made; Futurama/Slurm not assumed reachable |

## Operator decisions needed

1. (Done 2026-11-04) sccache 0.19.0 installed on bender.
2. Pilot project for the cold/warm benchmark.
3. TLS S3 endpoint, bucket, scoped 0600 read-only credentials, registry file (Stage 2),
   and approval to build the credentialed sccache server (the open gap).
4. Slurm: partition, account, and a verified runner (separate approval); settle the config overlap.
5. Metrics: whether to implement a Grafana/remote_write export.
6. Merge and push of `feat/fabric-build-cache`; deploy order herdr-eng then Pi extension.
