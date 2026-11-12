# Mission manifest (update per slice)

Branch: feat/agent-operations-map (worktree ~/IdeaProjects/herdr-engineering-wt/agent-ops-map), base 22ecf0f1.
Integration branch: feat/agent-operations-map only; no merge to main without user authorization.

| Slice | Owner | Files (exclusive) | Status |
|---|---|---|---|
| 00 baseline screenshots | sonnet | screenshots/baseline/ | running |
| 01 event schema + provenance | pi/sonnet | control_plane/envelope.py (additive), schemas/agent-ops-event.schema.json, tests | pending |
| 02 provider adapters | sonnet | emit.js, cp-feed plugin, pi hooks | pending |
| 03 rooms/handoff correlation | sonnet | control_plane/agent_ops.py (part) | pending |
| 04 aggregation/replay/authz | sonnet | control_plane/agent_ops.py, api.py routes | pending |
| 05 web map | codex/designer | packages/agent-ops-*.js, css | pending |
| 06 terminal map | codex/designer | herdr_engineering/agent_ops_tui.py, cli.py | pending |
| 07 ownership/WCC | designer | overview-view.js, session-context.js hooks | pending |
| 08 drift/context | sonnet | agent_ops.py (drift) | pending |
| 09 fleet/reconnect | sonnet | agent_ops.py (hosts) | pending |
| 10 a11y/perf/security + Opus review | opus reviewer | read-only | pending |

Each micro-spec lives in micro-specs/NN-*.md (objective, files, reuse, behavior, acceptance, tests, deps, rollback; <2k tokens).
