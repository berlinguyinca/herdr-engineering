# ADR-001 Agent Operations: architecture

Status: accepted (planner: Claude). Evidence: DISCOVERY-A-engineering.md, DISCOVERY-B-siblings.md.

## Decisions
1. **One collector, no new bus.** Agent Ops is a *projection* over existing sources:
   envelope v2 ingest (`POST /api/v1/ingest`, `control_plane/envelope.py`), per-room cursored
   events (`control_plane/collab.py`), handoffs (`handoffs.py`), inspector usage/model events.
   New module `control_plane/agent_ops.py` only reads/aggregates. No new storage engine or task DB.
2. **Provenance extension, not a new envelope.** Add optional fields to envelope v2
   (`provenance`, `host_id`, `session_id`, `model_id`, `parent_session_id`, `handoff_id`, `status`,
   `sanitized_label`) as additive/optional; absent = unknown. Never fabricate.
3. **Web**: vanilla ES modules, new `packages/agent-ops-{model,view}.js` + `agent-ops.css`, SVG
   rendering (as lot-map/command-mesh do), design tokens from `herdr-design-system/theme.css`,
   NAV entry in the Agents group, gated by capability `agent_ops_v1` (health endpoint).
   Reuse `team-map-model.js` edge classification. Reduced motion via `motion.js`.
4. **Terminal**: upstream Herdr has no in-TUI widget API; plugins add panes only.
   Terminal map = `herdr-eng agent-ops` renderer (Python, based on `pane_viewer.py`) run as a
   `[[panes]]` split via plugin manifest. Same projection API as web (consistency by construction).
5. **Live data from Herdr**: extend existing `herdr.cp-feed` plugin (pane.agent_status_changed,
   `report_metadata` tokens) -> ingest. No second plugin pipeline.
6. **Provider signals** (unknown stays unknown): Pi = hooks (model_select, getContextUsage);
   Claude = hooks, no model/ctx% -> unknown; Codex = notify + rollout JSONL (read-only, metadata only).
7. **Privacy**: metadata-only, redact labels/paths/errors, server-side room authz, fail-closed visibility.
8. **Replay**: deterministic fold of ordered events by (source, sequence/cursor); pure function
   `fold(events, t)` shared by tests.
9. **License**: lot-map / command-mesh are MIT; inspiration only, no code copied.

## Licensing/constraint notes
herdr-handoff has no LICENSE file: interoperate via its schema, copy nothing.
`npm test` hangs at full-suite level: use focused `node --test <file>` per slice.

## Rollback
Everything is additive behind `agent_ops_v1`; disable = capability off; revert = drop branch.
