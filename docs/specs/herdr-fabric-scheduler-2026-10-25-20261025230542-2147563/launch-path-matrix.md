# Launch-path matrix

| Test ID | Launch source | Current decision point | Remote launcher | Identity/linkage | Required policy enforcement |
| --- | --- | --- | --- | --- | --- |
| LP-01 | Dashboard create agent/session | `api.py:_api_post` and existing native integration handlers | `NativeHerdrAdapter.machine_targeted`, workspace/pane commands | session/mission/host fields plus events | scheduler reservation before native launch |
| LP-02 | `collab-spawn.py` fleet start | `Runner.process_one` → `pick_host` | `herd_pane`, `pane run`, SSH file transfer | collaboration `agent_id`, pane instance, session event | shared scheduler; room ACL does not grant host access |
| LP-03 | Collaboration child | `Runner.process_one` parent lookup | `herd_pane(... parent_pane)` | parent agent ID and pane graph edge | parent host is metadata/soft affinity, never implicit pin |
| LP-04 | Pi harness / child | `pi_autospec.py` and ingestion/session adapters | native Herdr session/pane | mission/session/parent session | scheduler adapter at agent creation boundary |
| LP-05 | Claude Code / Codex | family-specific process paths and collaboration commands | fixed family command through Herdr pane | family, agent/session, audit | family capability check and reservation |
| LP-06 | AutoSpec / Plannator | `pi_autospec.py`, `plannotator.py`, review store | existing planner/reviewer sessions | issue/mission/plan/review links | planner/reviewer separate workload requests |
| LP-07 | resumed/retried session | existing session state and command receipt paths | native resume/forwarding where supported | stable session ID and idempotency key | no duplicate launch when uncertain |
| LP-08 | remote external peer/A2A | collab auth/A2A service | no automatic host launch | graph identity only | external peer never becomes eligible owned host |
| LP-09 | scheduled jobs/builds | existing control-plane job/CI adapters | existing host/native runners | mission/project/artifact links | scheduler only for Herdr local work; InferWeave remains GPU owner |

Every row is covered by the P04 adapter contract tests. A route is not considered migrated until its request enters `PlacementRequest` and the bind path re-checks policy/telemetry generation.

