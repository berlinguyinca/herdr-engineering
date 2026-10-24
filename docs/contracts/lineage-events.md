# Lineage events (herdr-federated-engineering-20261022, T02)

Carried in envelope v2 (see event-envelope-v2.md). `entity_type = "lineage"`, `entity_id` = the subject node id.
Herdr projects them into the durable graph. **Lineage is only ever created by these explicit events** - never inferred
from chat text, pane layout, or timing.

## Node ids
Stable, prefixed, opaque: `<kind>:<id>`. Kinds: `room workspace mission plan task agent attempt session tab pane handoff
thread message decision approach worktree commit pr ci review finding artifact gate issue`.
`issue` ids are repo-qualified: `issue:github.com/<owner>/<repo>#<n>`; `pr` likewise (`pr:github.com/o/r#n`).
`agent:<id>` is the LOGICAL agent and survives session rollover and host migration; `attempt:<id>` is one worker run;
`session:<id>` one model session. A rollover adds a new `session` under the same `attempt`/`agent`; it never changes ids.

## Event types
| event_type | payload | meaning |
|---|---|---|
| `node.upsert` | `{node_type, node_id, label?, state?, attrs?}` | create/update a node; idempotent, last `source_sequence` wins per field |
| `node.state` | `{node_id, state, reason?}` | state change (e.g. running, blocked, done, failed, cancelled, superseded) |
| `edge.add` | `{edge_type, from_id, to_id, attrs?}` | typed directed edge; identity = `(edge_type, from_id, to_id)`, idempotent |
| `edge.remove` | `{edge_type, from_id, to_id, reason}` | tombstone (history kept; edge hidden after) |

`edge_type` in: `spawn handoff delegation consultation dependency issue_link fork supersession reviewed produced validated`.
Direction: `spawn` parent->child; `handoff` from->to; `delegation` delegator->delegate; `consultation` asker->answerer;
`dependency` dependent->prerequisite; `issue_link` any->issue/pr; `fork` origin->new approach; `supersession` new->old;
`reviewed` reviewer->subject (attrs.commit / attrs.plan_digest REQUIRED = revision binding); `produced` producer->artifact;
`validated` validator->subject (attrs.commit REQUIRED).

## Rules
- Every event carries the envelope `source`, `source_sequence`, `visibility_scope`; graph reads must filter by it.
- `reviewed`/`validated` edges are bound to an exact commit or plan digest; a newer head makes the older edge `stale`
  (kept, flagged), never silently valid.
- Unknown `edge_type`/`node_type` are rejected, not stored.
- Closing a pane/tab never emits a state change for the agent.
