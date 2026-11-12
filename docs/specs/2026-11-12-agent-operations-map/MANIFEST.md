# Mission manifest

Branch: feat/agent-operations-map (worktree ~/IdeaProjects/herdr-engineering-wt/agent-ops-map), base 22ecf0f1.
Integration branch: feat/agent-operations-map only; nothing pushed, no merge to main without user authorization.
Rollback: set `control_plane.agent_ops.enabled: false` (capability `agent_ops_v1` disappears, NAV entry hides, adapters stay inert without HERDR_AGENT_OPS=1); or drop the branches below.

| Slice | Branch | Status |
|---|---|---|
| 00 baseline screenshots | (in integration) | done (72 PNGs; collab/team pages have no populated baseline) |
| 01 event schema + provenance | feat/agent-operations-map | done |
| 03/04 rooms, handoff correlation, aggregation, replay, authz | feat/agent-operations-map | done; review fixes in feat/agent-ops-fix-backend |
| 05/07 web map, ownership, lineage | feat/agent-ops-web | done; review fixes in feat/agent-ops-fix-web |
| 06 terminal map + plugin manifest | feat/agent-ops-tui | done; review fixes in feat/agent-ops-fix-tui (manifest not validated with `herdr plugin link`) |
| 02 provider adapters | feat/agent-ops-adapters | done (handlers tested directly; no live Pi/Claude/Codex run) |
| 08/09 context health, drift, fleet hosts | feat/agent-ops-health | done |
| 10 review | Opus read-only | review 1 REJECT (3 blocking); fixes merged at 00daf836; re-review pending |

Test evidence at 00daf836 (focused): 166 Python tests pass (agent_ops, envelope, health, adapters, tui); 48 web unit tests pass.
Known flaky: test_pi_null_context_after_compaction_emits_compaction_not_a_metric failed once in a combined run and passed on 5 reruns; test_interactive_pane_in_a_real_pty_against_the_real_server is timing based.
