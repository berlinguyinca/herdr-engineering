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

## Always-on changes outside the `agent_ops_v1` flag
Two edits ship regardless of the flag, so "revert = capability off" does not undo them:
- `envelope.py` `normalize_envelope` validates the optional agent-ops fields of a v2 envelope. A v2 sender with an
  invalid optional ops field, or an `event_id` not matching `[A-Za-z0-9._:~-]{1,128}`, now gets a 400.
- `ingest.py` strips a client-sent `_envelope` key from the payload (server-owned metadata, never client-set).

## Trust model
API keys are shared and authenticate only; they do not identify a person. A node's identity is its session id.
Guest and external narrowing applies only to callers who send `Authorization`: anyone holding an `X-API-Key` who
omits it reads org scope. The narrowing is defence in depth, not a security boundary.

## Guest and external visibility
Fail closed: a guest, a lapsed guest, or an external agent reads `room:` chat sources of its allowed rooms and nothing
else (no org or private events, with the viewer key or the operator key). The trade-off: a lapsed guest sees an empty
map rather than the org map, even though their sign-in is still valid.
- "Lapsed guest" means the person's latest room membership was a guest one. A later non-guest add clears it
  (`_ever_guests` is rebuilt identically on log replay). Re-added as contributor then removed, they read org scope again.
- An agent inherits its owner's lapsed-guest status only when the owner reads no room other than guest rooms.
- An external agent is treated as a guest bound to its invited room; once the binding expires it sees nothing.
