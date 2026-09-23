# Herdr Compatibility Matrix

**Phase:** 0020 (Upstream Audit and Compatibility)
**Date:** 2026-09-23
**Reconciliation source:** `IMPLEMENTATION_RECONCILIATION.md`

This matrix replaces assumptions with observations of the actual installed
Herdr ecosystem. Machine-readable evidence snapshots are retained under
`docs/evidence/0020/`:

| Snapshot | Command | File |
|---|---|---|
| Version | `herdr --version` | `docs/evidence/0020/herdr-version.txt` |
| API schema | `herdr api schema --json` | `docs/evidence/0020/herdr-api-schema.json` |
| API snapshot | `herdr api snapshot --json` | `docs/evidence/0020/herdr-api-snapshot.json` |
| Machines | `herdr machine list --json` | `docs/evidence/0020/herdr-machine-list.json` |
| Plugins | `herdr plugin list --json` | `docs/evidence/0020/herdr-plugin-list.json` |
| Integrations | `herdr integration status` | `docs/evidence/0020/herdr-integration-status.txt` |

## Observed installed interface

Herdr **0.9.1** provides structured `machine list`, `plugin list`, `api schema`
and `api snapshot` JSON, `--machine` forwarding, and `integration status`. The
adapter in `herdr_engineering/herdr_adapter.py` negotiates capabilities from the
actual schema and degrades gracefully per-version rather than assuming a fixed
API. Capability negotiation is exposed as `herdr-eng capabilities --json`.

## Candidate upstreams (from `docs/upstream-baseline-2026-09-23.md` + research)

Every candidate is classified per spec 0020. The authoritative pinned
classifications live in `lock/upstreams.yaml`; this file is the human-readable
companion.

| Candidate | Repo / source | Classification | License | Maintenance | Notes |
|---|---|---|---|---|---|
| Herdr core | official binary (0.9.1) | **ADOPT** | proprietary/internal | active | Base primitive. Build on, never fork. |
| herdr-browser | `StructuPath/herdr-browser` | **ADAPT** | verify upstream | community | Browser preview; our `browser.py` adapts and falls back safely. |
| herdr-web | `eyalev/herdr-web` | **ADAPT** | verify upstream | community | Preferred web surface; `web.py` is the private stdlib fallback. |
| herdr-plannotator | `plannotator/herdr-plannotator` | **ADOPT** | verify upstream | active | Review authority; `PlannotatorAdapter` preserves review identity/decision/evidence. |
| herdr-swarm | upstream swarm | **OPTIONAL** | verify upstream | community | Optional parallel-agent helpers; not required for core integration. |
| herdr-worktreeinclude | `eightHundreds/...` | **OPTIONAL** | verify upstream | community | Worktree include helper; native worktrees may suffice. |
| herdr-file-annotator | `JonasBaeumer/...` | **OPTIONAL** | verify upstream | community | Line-anchored annotations; review semantics owned by Plannotator. |
| herdr-gh-checks | `itisbryan/...` | **OPTIONAL** | verify upstream | community | PR/CI checks in a pane; CI adapter already covers status. |
| herdr-pr-board | `cdowell09/...` | **OPTIONAL** | verify upstream | community | PR dashboard; not a core requirement. |
| herdr-notifications | `quinnjr/...` | **OPTIONAL** | verify upstream | community | OS notifications; `attention.py` provides policy/aggregation. |
| Custom remote-machine aggregator | (concept) | **SUPERSEDED_BY_NATIVE_HERDR** | n/a | n/a | Native saved machines + `--machine` forwarding suffice. |
| Custom generic worktree engine | (concept) | **SUPERSEDED_BY_NATIVE_HERDR** | n/a | n/a | Herdr owns worktrees. |
| Custom CI scheduler | (concept) | **SUPERSEDED_BY_NATIVE_HERDR** | n/a | n/a | Woodpecker/Pileated owns CI execution. |
| Custom LLM scheduler | (concept) | **SUPERSEDED_BY_NATIVE_HERDR** | n/a | n/a | Tern owns inference/GPU routing. |
| Pi-Web | external | **ADAPT** (link/embed) | external | active | Existing Pi UI; link/embed, never rebuild. |
| Pi Forge | external | **REJECT** | n/a | n/a | Explicitly excluded by the master program. |

## Architecture validation

Native Herdr saved machines and machine-targeted operations are verified as the
base fleet transport. A custom replacement would require a new ADR proving a gap;
no such gap exists, so the custom aggregator concept remains superseded
(see ADR-0002).

## Refresh rule

Before any dependency enters the Powerpack, it must be re-verified: repository
identity, canonical owner, latest compatible revision, license, build/install
hooks, network listeners, credential use, Linux/macOS support, a doctor/test run
against the actual Herdr version, then classification and pinning in
`lock/upstreams.yaml`.

## Exit gate

No dependency enters the Powerpack without a tested revision and classification,
and the implementation uses native Herdr primitives wherever they satisfy the
requirement. Compatibility tests for the adopted adapter surface live in
`tests/test_herdr_adapter.py`.
