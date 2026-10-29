# Pi session-ingest fields the Work Graph accepts (P02)

Status: **contract proposal from the Herdr side, implemented and tested in herdr-engineering** (`work_graph_core._session_ops`,
`tests/control_plane/test_work_graph_core.py::test_real_session_ingest_rows_carry_...`). The pi-engineering agent that makes
`ControlPlaneIngest.session` send these ids should use exactly these names. Every field is **optional**; absence is tolerated
(the session simply has less linkage and, with no scope at all, stays private to its writer).

Transport: `POST /api/v1/ingest` envelope v2, `entity_type: "session"`, `entity_id: <session id>`,
`event_type: "session.started" | "session.stopped" | "session.created" | "session.updated"` (the server also derives
`SessionStarted`/`SessionStopped` rows; they carry none of these ids and the Work Graph treats them as scope-less).

| Field | Where (either) | Meaning | Graph effect |
|---|---|---|---|
| `room_id` | envelope column (preferred) or payload | HCN room the session works for | scope: the session is visible to non-guest members of that room; `room_ids` |
| `workspace_id` | envelope column or payload | Herdr workspace/worktree | `attrs.workspace_id`; fact attribute |
| `agent_id` | envelope column or payload | the **HCN agent id** (`agent_enrolled.agent_id`) | `agent` node ensured; fact subject `agent:<agent_id>` |
| `agent_instance_id` | **payload** | the Pi process/instance (changes on restart; not an identity) | `attrs.agent_instance_id`; recorded on the historical fact; fact subject `agent_instance:<id>` when no `agent_id` |
| `mission_id` | envelope column or payload | Pi mission | edge `mission spawned agent_session` |
| `task_id` | envelope column or payload | the durable task (same id as the HCN room task) | edge `task spawned agent_session` |
| `project_id` | envelope column or payload | HCN project | `project_id` on the node |
| `visibility_scope` | envelope | `private|mission|room|org` | scope; `room` without `room_id` degrades to `private` |

Rules the Pi side can rely on
* Idempotent: re-sending an event (same actor + `event_id`) is a no-op; out-of-order delivery converges to the same graph.
* A session is a **historical fact** once it has started: stop closes the interval, nothing is deleted when the agent exits.
  Live presence (heartbeats, "is it typing") is NOT recorded here.
* Pi must keep sending the ids on **every** session event for the session; the fold takes scope from events that state scope
  and ignores scope-less ones, so a later event without the ids does not lose the room.
* No secret, prompt text or transcript belongs in these payloads (they are stored in `fabric_events` as sent).
* `trace_id` (new envelope field, migration 0031) may be sent for correlation; it is stored and indexed, nothing depends on it.
