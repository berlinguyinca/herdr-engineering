# Implementation Reconciliation

**Repository:** `berlinguyinca/herdr-engineering`
**Date:** 2026-09-23
**Phase:** 0010 (Repository Bootstrap and Reconciliation)

This document maps every major requirement of the Super HerdR master program to
its current status in this repository and the surrounding ecosystem, using the
classification vocabulary required by spec 0010:

`EXISTING` · `PARTIAL` · `MISSING` · `CONFLICTING` · `SUPERSEDED` · `EXTERNAL`

"EXTERNAL" means the capability is owned by another system (Herdr, Pi
Engineering, AutoSpec, Tern, Woodpecker/Pileated, Plannotator, Tailscale,
GitHub) and must **not** be re-implemented here.

---

## 1. Environment reconciled on 2026-09-23

| Component | Observed | Status |
|---|---|---|
| Herdr binary | `herdr 0.9.1` at `~/.local/bin/herdr` | EXTERNAL (base primitive) |
| Herdr schema | captured `herdr api schema --json` → `docs/evidence/0020/herdr-api-schema.json` | EXISTING |
| Herdr machine list | `herdr machine list --json` → `docs/evidence/0020/herdr-machine-list.json` | EXISTING |
| Herdr plugin list | `herdr plugin list --json` → `docs/evidence/0020/herdr-plugin-list.json` | EXISTING |
| Herdr integration status | `herdr integration status` → `docs/evidence/0020/herdr-integration-status.txt` | EXISTING |
| Pi | `pi 0.87.0` | EXTERNAL |
| Python | 3.12.3 (implementation language) | EXISTING |
| Node | 22.23.2 | EXISTING (optional tooling) |
| Go / Rust / Java | go 1.27.1, cargo 1.97.0, OpenJDK 21 (test adapters) | EXISTING |
| Git | 2.43.0 | EXISTING |
| Tailscale | `/usr/bin/tailscale` present; not currently up | PARTIAL (reachability gate) |
| Ansible | **not installed** on this workstation | PARTIAL (playbooks provided, convergence external) |
| gh | 2.100.0 | EXISTING |
| Docker | available | EXISTING (optional) |

## 2. Requirement-by-requirement reconciliation

### 2.1 No duplicate remote-machine orchestrator
Native Herdr saved machines + `--machine` forwarding satisfy the base fleet
transport. `herdr_engineering/herdr_adapter.py` is a **typed adapter over**
native Herdr, not a second orchestrator. A custom aggregation layer was a
candidate in the archived 2026-09-21 fleet spec; it is **SUPERSEDED** by native
Herdr (see `docs/decision-history.md` and ADR-0002).

### 2.2 No duplicate generic worktree engine
Herdr owns worktrees. `NativeHerdrAdapter.list_worktrees()` / `create_worktree()`
forward to Herdr. The archived custom worktree helper concept is **SUPERSEDED**.

### 2.3 No second CI scheduler
`herdr_engineering/ci.py` is a read/action **adapter** over Woodpecker/Pileated.
EXTERNAL authority retained. Stale/unavailable is shown, never inferred success.

### 2.4 No second LLM scheduler
Tern (InferWeave) owns inference via `https://llm.metabolomics.us`. HerdR
Engineering does not schedule models/GPUs. EXTERNAL.

### 2.5 No second agent/mission orchestrator
Pi Engineering owns missions/workers/lifecycle. `pi_autospec.py` only **consumes**
typed lifecycle events and correlates identifiers. EXTERNAL authority.

### 2.6 No second review engine
Plannotator owns review semantics. `browser.py` `PlannotatorAdapter` preserves
review identity/comments/decision/evidence. EXTERNAL authority.

### 2.7 Automatic workspace naming
Implemented as `herdr_engineering/naming.py` — `derive_workspace_identity()` with
provenance, collision-safety, user-rename preservation and at-most-one
refinement. **EXISTING** (spec 0010 requirement). Dogfooded via `herdr-eng name`.

### 2.8 Contracts
All five contracts implemented as typed dataclasses in
`herdr_engineering/contracts.py` (ActivityEvent, ArtifactRef, DevServiceLease,
TestEvent, CIEvent). **EXISTING**.

### 2.9 Fleet Ansible
`ansible/` playbooks provided for Linux + macOS convergence of
`fry`, `beast`, `bender`, `macbook-m4`. Actual convergence of the remote
validation fleet is **EXTERNAL / BLOCKED** (hosts not reachable from this
workstation). Playbooks are real and idempotent by design; convergence evidence
is pending fleet access.

### 2.10 Web/mobile control surface
`herdr_engineering/web.py` is a private-by-default stdlib HTTP server with a
mobile-first responsive UI. **EXISTING**. `herdr-web` upstream is **ADAPT** lead.

### 2.11 Browser preview + Plannotator
`herdr_engineering/browser.py` — Playwright screenshot capture as ArtifactRef,
safe fallback, Plannotator review adapter. **EXISTING** (live Chromium/CDP
validation is EXTERNAL/BLOCKED without a reachable browser + target app).

### 2.12 Shared artifact workspace
`herdr_engineering/artifacts.py` — abstract provider + filesystem provider,
atomic publish, content hashing, provenance, retention, secret exclusions,
degraded queue on storage loss. **EXISTING**.

### 2.13 Session journal/timeline
`herdr_engineering/journal.py` — idempotent, out-of-order-safe, persistent,
newest-first timeline + Started/Completed/Current/Next summary. **EXISTING**.

### 2.14 Transparent dev fabric
`herdr_engineering/devfabric.py` — lease registry with atomic collision-free
port allocation, TTL/heartbeat/reconcile, idempotent registration, private-only.
**EXISTING**. Live multi-host Tailscale route validation is EXTERNAL/BLOCKED.

### 2.15 Test explorer
`herdr_engineering/tests.py` — pytest/Go/Cargo/JUnit/Scala adapters emitting
TestEvent, normalized tree, native-command rerun. **EXISTING**.

### 2.16 CI workspace
`herdr_engineering/ci.py` — Woodpecker-compatible provider, pipeline views,
"Debug with Pi" handoff. **EXISTING** (live Woodpecker instance EXTERNAL).

### 2.17 Candidate/worktree comparison
`herdr_engineering/candidates.py` — candidate model + safe owner-guarded
cleanup. **EXISTING**.

### 2.18 Unified UX
`herdr_engineering/web.py` navigation surfaces Fleet/Sessions/Activity/Dev/
Tests/CI/Attention with shared context. **EXISTING** (deep unified IA is
PARTIAL — see spec 0150).

### 2.19 Attention/notifications
`herdr_engineering/attention.py` — coalescing attention registry, policy,
quiet hours, dedupe, owner-system action execution. **EXISTING**.

### 2.20 Observability/security/doctor
`herdr_engineering/observability.py` (metrics + public-listener detection),
`herdr_engineering/doctor.py` (`herdr-eng doctor --json`). **EXISTING**.

### 2.21 Powerpack distribution/lifecycle
`herdr_engineering/powerpack.py` — lock, enable/disable policy, snapshot,
preflight, rollback. `lock/upstreams.yaml` populated. **EXISTING**.

## 3. Repository ownership of required changes

| Change | Owning repository | Implemented where |
|---|---|---|
| Base fleet transport | Herdr | `herdr_adapter.py` (adapter only) |
| Mission/worker lifecycle | Pi Engineering | `pi_autospec.py` (consumer only) |
| Spec/issue workflow | AutoSpec | `pi_autospec.py` (consumer only) |
| Model/GPU routing | Tern | config points to `https://llm.metabolomics.us` |
| CI execution | Woodpecker/Pileated | `ci.py` (adapter only) |
| Review semantics | Plannotator | `browser.py` (adapter only) |
| Private network | Tailscale | `devfabric.py`, `web.py` (private-only) |
| Git truth | Git/GitHub | worktrees/commits/PRs |

## 4. Conflicts vs archived specs

- Archived `berlinguyinca/herdr-powerpack` (2026-09-22) planned a second
  repository. **SUPERSEDED** by the canonical `herdr-engineering` repo
  (ADR-0001). "Powerpack" remains the distribution concept here.
- Archived custom remote-machine aggregator (2026-09-21) is **SUPERSEDED** by
  native Herdr saved machines (ADR-0002).
- Archived `2026-09-20` Pi↔Herdr runtime spec is honored: Herdr is external;
  Pi Engineering keeps orchestration; large communication is artifact-first.
- No archived spec conflicts with the current normative specs 0010–0180.

## 5. Exit gate

Every master requirement traces to a current owner/status above. No
implementation phase relies on an unidentified repository or a guessed
pre-existing API — the Herdr schema is captured under `docs/evidence/0020/`.
