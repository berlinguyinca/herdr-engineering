# Architecture, ADR and implementation plan

## ADR-1: Assistant ownership
- **Status:** accepted for implementation, 2026-10-29.
- **Decision:** `herdr_engineering/control_plane/assistant.py` is the only assistant backend. It is a mission participant that observes, explains and proposes. It never owns or schedules missions. Rakazo is not deployed (see `rakazo-fit-gap.md`).
- **Consequences:** reads go through `read_graph` with the run's principal (never model-supplied); writes reuse `request_tool_action` approvals. Tool permissions never depend on the selected model.

## Authority (unchanged from the spec)
Pi executes and fences; AutoSpec owns the issue DAG, reviews and merge eligibility; Herdr projects and gates; Git/CI is revision truth; Artifact Studio holds blobs; OpenViking is memory only. No second scheduler, store, graph DB or dashboard.

## Plan, dependency-ordered
| Step | Content | Repo | Gate |
|---|---|---|---|
| S0 | Assistant hardening: self-preauthorized grants, empty allowlist, connector bypass, principal-bound request hash | herdr-engineering | unit tests incl. negative authorization |
| S1 | `read_lineage` read-only assistant tool over `read_graph`, with freshness/uncertainty fields; behind existing flag; no grants by default | herdr-engineering | scope-isolation tests, cross-room 404 |
| S2 | Artifact commit to graph: emit commons `artifact` node and `produced` edge with immutable version hash | herdr-engineering | restart/replay test |
| S3 | Pi: real handoff transition callers, `session` to `session` handoff edge, single context-threshold config (70/80/90 as proposals), `room_id` on emitted events | pi-engineering | 10-handoff sequence test |
| S4 | Checkpoint contents + verify-before-revoke + runaway-process quarantine | pi-engineering | crash-at-each-stage tests |
| S5 | Inspector fields, artifact links, pan/zoom, Missions/assistant baselines | herdr-engineering | ui-gates |
| S6 | Pi `/room` thin client; assistant request adapters (`request_review`, `propose_branch`, `comment_on_node`) with TTL and dry-run | both | negative-authz tests |
| S7 | CI-failure to repair to re-review loop wiring via AutoSpec quorum | autospec + herdr | exact-SHA tests |
| S8 | Fault matrix, large graph, metrics, runbooks | all | evidence in ledger |

First real vertical slice (spec P1): S3 handoff transition -> durable Herdr event -> graph -> artifact link (S2) -> assistant read-only summary (S1). S0 comes first because widening the assistant on top of the current grant bypass would be unsafe.

## Rules binding new code (from constitution/baselines/design/AGENTS.md)
YAML-only config, no environment variables; no secrets in git; stage files by name; conventional commits; design tokens only (no raw colors, 4/8/12/16/24 spacing scale, reduced-motion fallback, 320px reflow); independent reviewer differs from author; no merging without gates.
