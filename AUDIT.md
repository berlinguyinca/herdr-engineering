# AUDIT — Herdr Fabric Build Cache (micro-spec 00)

Read-only audit. No source edited, nothing deployed, no remote host touched.

## Provenance
- Execution host: `bender` (Linux). The spec ZIP was in bender's `~/Downloads`; SHA-256 verified `403146d7…e057`.
- Repo: `berlinguyinca/herdr-engineering`. Work tree `~/herdr-engineering-wt/fabric-build-cache`, branch `feat/fabric-build-cache`, from `origin/main` 0ab6931c.
- The pre-existing checkout `~/herdr-engineering-repo` was NOT used: 913 commits behind, with an untracked `deploy/control-plane/deploy.sh`. Left untouched.
- Spec bundle installed at `docs/specs/herdr-fabric-build-cache-20261104/` (no collision). Detailed reports: `…/audit/{A,B,C}-*.md`.

## Existing functionality (reuse, do not rebuild)
| Area | What exists | Where |
|---|---|---|
| Build gate | `build-coordinate`: `gate.N` flock slots + shared `build-maintenance.lock` (LOCK_SH builds / LOCK_EX cleanup); execs child | `build_coord.py` |
| Cleanup | Removes only old untracked Cargo `target/` dirs under configured roots; /proc scan + exclusive lock; dry-run; systemd user loop (900 s) | `build_cleanup.py`, `build_maintenance.py`, `ansible/tasks/build-cleanup.yml` |
| Pi gate | Wraps `cargo check/build/test/clippy` and `tsc`, anchored at command start; sets no env | `deploy/pi-build-gate/build-gate.ts` |
| API | `route_*` modules under `_route_api` (`api.py:3042`); capability strings; role keys; redaction on all SSE payloads | `control_plane/api.py`, `redact.py` |
| Preview→confirm | pending row + `{"confirm":true}`, token shown once | `artifact_share_api.py` |
| Default-off flag | fail-closed config, secrets by file path | `skill_routes.py`, `config.py` |
| Idempotency | key + request hash | `collab_intents.py` |
| SSE | `/api/v1/events/stream`, publish via `repo.record_event` | `api.py` |
| Dashboard | Fabric/Hosts tabs (`app.js` `SECTION_TABS`, `NAV`), `hosts-view.js`, `host-detail-view.js`; ui-gates (a11y, tokens, xss, states, visual) | `packages/` |
| Slurm | read-only `sinfo` capacity probe only | `control_plane/slurm_placement.py` |

## Missing (confirmed by grep: zero hits for sccache / RUSTC_WRAPPER / CARGO_TARGET_DIR / CARGO_INCREMENTAL)
sccache wrapper; durable build identity & lifecycle states; leases with epoch/fencing; cache-aware host selection; shared S3 cache; physical-write telemetry; `/api/v1/build-cache/*`; build-cache SSE events and table (next migration 0046); dashboard panel; Prometheus exporter / Grafana push (none in repo); Slurm job runner (none).

## Findings that change the spec
1. **Slurm "runner" does not exist.** Placement is read-only; per spec 11 and matrix row 10 this does not count as launch capability. No Slurm binaries on bender. Spec 11/12 stay blocked until a real, approved runner and test partition exist.
2. **`sccache` not installed** on bender (rustc/cargo 1.97.0). Latest upstream v0.19.0 (2026-10-24) has native multi-level `disk → s3` cache, so no custom proxy. Installing it is a host change → needs operator approval; until then tests use a fake `sccache` executable only for argument/env contract checks, never to claim savings.
3. **Not cacheable:** bin/dylib/cdylib/proc-macro crates; `CARGO_INCREMENTAL=1` makes sccache error → disable incremental only in validated cacheable profiles. Build-script cacheability, JSON stats fields, and first multilevel version are UNVERIFIED — verify against the installed binary.
4. **"Physical writes" metric:** repos, `~/.cargo`, `/tmp` share one NVMe under ZFS (77 % full). `/proc/<pid>/io` `write_bytes` is logical. Report it as *logical process writes*; physical needs `/proc/diskstats`/ZFS counters (host-wide, noisy). Baselines must label which.
5. **No deployed shared S3 backend.** Only a loopback MinIO (named "rustfs") in `deploy/control-plane/compose.yaml`. VersityGW/Garage appear nowhere. Shared cache (spec 04/06) cannot be validated across hosts without an operator-chosen endpoint.
6. **"Futurama"** appears only in the spec bundle. No evidence of its address or role in the repo; do not assume reachability.
7. **Doc contradiction resolved:** `docs/build-gate.md:137-140` ("20 on 24 cores") is stale; the cap of 4 (`build_coord.py:67`, `max(2, min(4, cores-4))`, tests `test_build_coord.py:32-46`) is true. Stale text also in `cli.py` `--slots` help and the `build-gate.ts` header (fix is in scope of spec 05).
8. SSE has no `Last-Event-ID`/`id:`; replays last 100 events. Spec 09 "resume" needs either that to be added to the shared stream (touches other features) or documented replay-window semantics.
9. Load-guard loop ignores the `--wait` deadline → a build can hang under sustained load. Existing hazard; do not worsen.

## Ownership
Herdr Engineering: all new code. Pi Engineering (`~/pi-engineering`): consumer only; edits limited to the Pi gate file already in this repo. woodpecker-slurm / hive CI: audit only. InferWeave: untouched.

## Risks
- Cleanup TOCTOU for builds that bypass the gate; leases must be rechecked under the exclusive lock.
- `exec` in build-coordinate means no post-build hook; recording completion needs a supervising wrapper (behaviour change → behind flag).
- ZFS 77 % full: any eviction work needs dry-run-first.
- `api.py` is 6249 lines: add a separate `route_build_cache` module, minimal registration diff.
- UI gates (tokens/xss/visual) will fail on new views that skip design tokens or escaping.

## Baseline
`python3 -m pytest tests/test_build_coord.py tests/test_build_cleanup.py tests/test_build_maintenance.py tests/test_cleanup_deploy.py -q` → **63 passed**.

## Go / no-go per micro-spec
| Spec | Decision |
|---|---|
| 01 measure | GO (no sccache needed; typed events, CPU/elapsed, /proc io) |
| 02 config | GO |
| 03 local | GO for wrapper code + tests; **BLOCKED** for on-bender warm-hit proof until sccache install approved |
| 04 storage | GO for config generation/validation/fallback logic; live S3 canary BLOCKED (no endpoint) |
| 05 leases | GO |
| 06 fabric | GO for inventory/fingerprint; two-host reuse proof BLOCKED |
| 07 policy | GO (dry-run default; no destructive run) |
| 08 pi | GO |
| 09 api | GO |
| 10 ui | GO |
| 11 slurm | BLOCKED (no runner/partition/approval) — only adapter interface + fake-free contract tests if feasible |
| 12 dist | BLOCKED (Linux/bubblewrap/root, Slurm) — evaluation doc only |
| 13 chaos | PARTIAL (local-only scenarios) |
| 14 rollout | Docs/report only; no deployment |
