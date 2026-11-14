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
| 10 review | Opus read-only | R1 REJECT (3 blocking) -> R2 REJECT (cursor) -> R3 APPROVE (03e516c8) -> R4 REJECT (compound pass/pwd redaction regression) -> R5 APPROVE (fixes b46fd4ee; no blockers) |
| fix rounds | feat/agent-ops-fix-2/-3/-4, -shots, -visual, fix/agent-ops-round4 | all merged; visual pass 50485f3b; round-4 fixes merged 2f650b7f |

Test evidence at 00daf836 (focused): 166 Python tests pass (agent_ops, envelope, health, adapters, tui); 48 web unit tests pass.
Known flaky: test_pi_null_context_after_compaction_emits_compaction_not_a_metric failed once in a combined run and passed on 5 reruns; test_interactive_pane_in_a_real_pty_against_the_real_server is timing based.

Test evidence at final tip: 258 Python (same five files), 65 node, tests/playwright/agent-ops.test.mjs 12/12. Earlier at 5cc60e00: 240 Python tests (agent_ops, envelope, health, adapters, tui; two clean runs), 61 node tests (model 36, view 21, lineage 4), tests/playwright/agent-ops.test.mjs 11/11.
Known flaky: test_interactive_pane_in_a_real_pty_against_the_real_server (timing); a Claude-hook adapter test once under load. nav-groups.test.mjs and calm.test.mjs fail/hang on the pre-mission base 22ecf0f1 too.

## Authz follow-up (after round 5)
- MERGED: guest-only viewers see only `room:` sources of their guest rooms (8ef5d6b1 + test fd9ea17e; red without the fix, green with it; Opus review found no leak path).
- NOT MERGED: per-actor node identity, branch fix/agent-ops-authz (tip eb6a4129, in worktree agent-ops-fix-6). Three Opus reviews rejected it (forged `@ref` takeover; reference ids merging nodes; plain-id ownership derived from the windowed event set can flip). Not a regression vs this branch's tip (which merges all keys into one node) and leaks no data, but the design is incomplete: agent ids are free-form and no key owns them. Proper fix = server-side claim of an agent id by its first authenticated key at ingest, persisted, not recomputed in fold.
- Trust model (confirmed by operator): API keys are shared between agents and authenticate only; they do not identify. Node identity is the session id (Pi session id or host/pid/boot hash, Claude session_id, Codex thread, herdr host:server:pane), never the key, so shared keys do not merge nodes. Per-key ownership (fix/agent-ops-authz) is therefore unnecessary and stays unmerged. A sender with a valid key can still claim any session id; accepted under this model.
- FIXED 9c5cca8e: a lapsed/revoked guest kept a valid sign-in and fell back to org visibility (confirmed leak, red then green). Now sees nothing on snapshot/events/replay/stream. Trade-off: anyone ever added as a guest who reads no other room is denied permanently.
- FIXED 5add21bf (layout): node text ellipsised inside the circle, edge labels kept off other edges' lines, 80-col legend fits. Screenshots: screenshots/after-web-final3/, after-terminal-final/. Remaining: some labels sit ~40px from their own line; phone width hides most edge labels until tap; long names show as "Claude (...".
- Live providers (2a5fa646, evidence/live-providers/): Claude and Codex ran real turns against a local server (node keyed by real session id; Codex model+context real; Claude model/ctx unknown as expected). Pi extension loaded and emitted session events but no model turn (needs credentials not copied). `herdr plugin link` succeeded against a throwaway herdr home only; pane/action launch not exercised (needs a running herdr server).

