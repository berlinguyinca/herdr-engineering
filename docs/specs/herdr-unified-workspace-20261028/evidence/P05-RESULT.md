# P05 result: PARTIAL (library slice only)

Spec: `phases/P05.md`. Gate: assigned task executes once across retry/crash; independent review enforced; aborted
execution does not leak privileges; task result links back to room/project/issue.

## Delivered (herdr-engineering, `team_tasks.py`, flag off, nothing calls it)
| Spec requirement | Where | Evidence |
|---|---|---|
| Reusable team templates without host pinning | `template_from_dict` | `test_a_bad_template_is_refused_for_the_stated_reason` (host keys refused) |
| Room/team submits only via authorized actor/broker; messages never become tasks | `admit` (+ P01 `Broker.authorize("submit_task")`) | `test_a_message_or_tainted_text_never_becomes_a_task`, `test_an_authorized_actor_may_submit...` |
| Bounded budget, role, deadline, allowed actions, dependencies | `admit` | `test_bounds_deadline_budget_and_actions_are_enforced`, `test_dependencies_must_exist_and_cannot_cycle` |
| Reviewer separation | `admit` + reviewer read-only in `template_from_dict` | `test_the_reviewer_must_be_independent...` |
| Lease/fencing, executes once across retry/crash | `LeaseStore` | `test_a_task_runs_once...`, `test_after_a_crash_the_successor_wins...` |
| Aborted execution leaks no privileges | `Lease.authorizes`, `LeaseStore.abort` | `test_an_aborted_execution_keeps_no_privileges` |

Mutation check: removing the token fence, the origin check, reviewer independence, or lease revocation on completion
each fails a test (run by hand, then restored).

## Wired: independent review at the real task chokepoint
`CollabService.create_task` takes `reviews_task_id` and enforces reviewer independence against the room's real task records (`tests/control_plane/test_collab_review_independence.py`, 7 tests; each of the three rules mutation-checked; 1,626 collab tests pass). Not yet: the work graph does not draw a `reviews` edge from it, and the lease store is not used by accept/complete.

## Durable leases (migration 0040, `team_task_leases_pg.py`)
The lease contract also runs against Postgres (`tests/control_plane/test_team_task_leases_pg.py`, 5 tests incl. a 12-way concurrent acquire with exactly one winner). Mutation check: making takeover unconditional fails 3 of 5. Rollback: `DROP TABLE team_task_leases` (see the migration's `-- DOWN:`).

## Gate status
Library-level gate behaviour is demonstrated by unit tests only. The gate as written is NOT met end to end:
nothing routes a task through this, no route uses either store, and "task result links back to room/project/issue" is not implemented.

## Not done
`/assign`, `/tasks`, `/skill`, `/session attach`, steer, cancel; Pi authoritative mission admission; room-to-mission
binding; session migration and fabric scheduler placement with exclusions; skill execution with audited artifacts;
UI. Agents stay unable to submit tasks (decision recorded in P01 answers).

## Rollback
Delete `team_tasks.py` and its test; no schema, route or config was added.
