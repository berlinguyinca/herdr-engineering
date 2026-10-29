# P01 result: canonical identity, contracts and authorization

**Status: COMPLETED for the P01 gate as a contract/library phase (schema fixtures, permission matrix, adversarial tests, consumer compatibility).**
**Not delivered, by design:** no live route calls the broker, no persistence, no migration. Those are P02+.
Branch `feat/uw-p01-contracts` off origin/main 676b442 (PR #364). Spec snapshot copied from `spec/unified-workspace-20261028` @ 543f787216b190da8f7f6a3b5d1c4b0261a49100 (SHA256SUMS verified; `audit/` and `evidence/P00-RESULT.md` are frozen copies from that commit).

## Deliverables (all paths in the repo)
| Item | Path |
|---|---|
| JSON Schema 2020-12 (14 kinds, event, edge with 14 types) | `schemas/workspace-v1.schema.json` |
| Contract + graph mapping doc | `docs/contracts/unified-workspace-v1.md` |
| Module (ids, validators, CAS, idempotency, broker, taint, ACL hooks, cursor) | `herdr_engineering/control_plane/workspace_contracts.py` |
| Fixtures (14 entity, 14 edge, 1 event valid; 13 schema-invalid with expected keyword; 7 semantic-invalid) | `tests/control_plane/fixtures/workspace_v1/` |
| Tests | `tests/control_plane/test_workspace_contracts_{schema,broker,acl,replay}.py` |
| Flag | `control_plane.unified_workspace.enabled` (YAML, default off) -> health capability `unified_workspace_v1` (`api.py`, `cli.py`) |

## Existing vs added (graph.py)
Existing node types reused: room, mission, issue, task, agent, session (agent_session), host, artifact, review, decision. Added by P02: project, team, artifact_version, external_action. Existing edges reused: assigned_to, implements, produced, references, plus aliases depends_on->dependency, delegated_to->delegation, spawned->spawn, reviews->reviewed, supersedes->supersession. Added by P02: contains, derived_from, discussed_in, approved_by, notified_via. graph.py is NOT modified (asserted by `test_live_graph_vocabulary_is_unchanged_by_p01`).

## Test evidence (actual output)
New tests:
```
$ python3 -m pytest tests/control_plane/test_workspace_contracts_*.py -q
112 passed in 3.92s        (schema 72, broker 25, ACL 8, replay 7)
```
Subset (collab, graph, artifact, assistant, lineage views), run with the changes in place:
```
2 failed, 2152 passed, 1 deselected, 1 xfailed in 775.49s
FAILED test_collab_helper_runner.py::test_a_paused_helper_stops_even_when_the_fleet_path_fails
FAILED test_collab_public.py::test_collab_only_serves_public_surface_and_survives_restart
```
Both pass when re-run alone (2 passed in 2.52s): load-dependent, unrelated to this change.

Full suite (`python3 -m pytest -q`, the Python half of `scripts/ci/unit-gates.sh`):
```
3 failed, 8201 passed, 59 skipped, 129 deselected, 1 xfailed in 2641.25s (0:44:01)
FAILED test_collab_pentest.py::test_jwt_claim_attacks_rejected[over12]   (wall-clock based JWT lifetime check)
FAILED test_collab_public.py::test_collab_only_serves_public_surface_and_survives_restart
FAILED tests/test_fabric_cli.py::test_gateway_tls_flags_serve_https_and_both_flags_are_required
```
Re-run of those three modules in full: `303 passed in 196.59s`. So the 3 failures did not reproduce; they are timing/load flakes (a 44 minute run on a loaded workstation), not regressions. Not proven flaky-on-main by a baseline run.

## Gate verdict per the P01 gate
- Schema contract fixtures: passed (valid fixtures pass the schema and the semantic validators; each invalid fixture fails on its named keyword).
- Permission matrix: passed (hand-written role truth table, agent/room-grant/artifact cases, monotonicity over all grant subsets, viewer/guest never mutate). A08 covered.
- Adversarial: passed (forged membership, delegation laundering, injected attachment text A11, tool-result taint, stale/replayed/expired/unbound approvals, approval after revocation or demotion, idempotency conflict, in-doubt external send A10, agent/self approval).
- Consumer compatibility: passed (artifact-report-v1 schema untouched and valid, events.Event v1 and v2 project to the event contract, AssistantGrant adapts to AccountGrant, graph vocabulary unchanged, cursor contract pinned against the real CollabService).

## Not verified / limitations (honest)
- `scripts/ci/unit-gates.sh` itself was NOT run (needs a fresh venv, `npm ci` and Playwright download); only its pytest half was run directly in the workstation environment. No JS changed. Woodpecker status for the PR is reported separately in the PR.
- A09's "guest" is modelled with graph.can_see on server-built member principals; end-to-end HTTP guest traversal does not exist until P02 wires routes.
- Guest-hidden helper events advancing the cursor are described in the doc from the code (`read_events`) but not re-tested here; existing helper tests cover helpers.
- The broker is library-only and in-memory (approvals, idempotency, audit). Durable stores are P02/P07.
- `trace_id` is optional on events: `events.py` has no such column (P02 gap).

## Rollback
Flag off (the default): capability unadvertised, nothing calls the module. The change is additive (new schema file, new module, new tests/fixtures/docs, one optional `ControlPlaneServer` kwarg, `jsonschema` in the dev extra). Reverting the PR removes it with no data impact; there is no migration.
