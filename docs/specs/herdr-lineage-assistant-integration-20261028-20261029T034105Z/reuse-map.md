# Reuse map

Verbs: **reuse** unchanged, **extend** add to existing code, **refactor** change structure, **new** thin addition only.

| Need | Existing component (path) | Action |
|---|---|---|
| Event envelope and ingest | `control_plane/api.py:1721`, `docs/contracts/event-envelope-v2.md` | reuse; no `lineage.v3` |
| Graph projection and reads | `control_plane/graph.py` `read_graph` (`:2331`) | reuse; assistant calls it with the run's principal |
| Lineage emitter (Pi) | `pi-engineering src/runtime/herdr/lineage.ts` | extend: add `room_id`, wire real handoff transitions |
| Handoff state machine (Pi) | `missionStore.ts:2357,2424`, `runtime/host/handoffKinds.ts` | extend: add production caller; fold `plannerWorker/handoffCommand.ts` onto it |
| Checkpoint (Pi) | `orchestration/checkpoints.ts` | extend: add untracked snapshot, test split, decisions, next action, memory refs; delete unused `resilience/checkpoint.ts` after confirming no importer |
| Context trigger | four constants (see audit) | refactor into one config with soft/preferred/emergency |
| Fencing/leases | `orchestration/ownership.ts`, `lanes.ts` | reuse; add process quarantine |
| Lineage UI | `packages/collab-lineage.js`, `collab-view.js` | extend: inspector fields, artifact links, pan/zoom |
| Rooms and messages | `control_plane/collab.py` | reuse |
| Artifact versions and ACL | `control_plane/artifacts.py`, `artifact_acl.py` | reuse; extend `_promote`-style `produced` edge to artifact commits |
| Assistant | `control_plane/assistant.py`, `packages/personal-assistant-view.js` | extend: `read_lineage` tool first; later thin adapters |
| Project intelligence read tool | `intel_assistant.py`, `project_intelligence_api.py:136` | reuse as the pattern for scoped reads |
| Review quorum, merge gate | `autospec review_quorum/*`, `merge_gate.rs` | reuse; do not re-implement in Herdr |
| Issue/task DAG | `autospec-core initiative/dag.rs` | reuse by reference (`owner/repo#N`), per T05 of `herdr-federated-engineering-20261022` |
| UI tests and baselines | `tests/playwright/*`, `scripts/ci/ui-gates.sh` | extend with Missions/assistant baselines |
| Metrics | `autospec-core cost/*`, `aar/telemetry.rs` | reuse; "accepted changes per token" is new and reported as unknown until sources report |
| Local Pi room TUI | none | new, thin client over existing `/api/v1/collab` |
| Connector secret store | none (plain objects) | new, only when Gmail is wired; follow Rakazo envelope design as reference |
