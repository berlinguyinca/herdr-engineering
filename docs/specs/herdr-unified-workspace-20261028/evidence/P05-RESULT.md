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

## Gate status
Library-level gate behaviour is demonstrated by unit tests only. The gate as written is NOT met end to end:
nothing routes a task through this, no persistence (the in-memory `LeaseStore` is the contract the Postgres store must
match), and "task result links back to room/project/issue" is not implemented.

## Not done
`/assign`, `/tasks`, `/skill`, `/session attach`, steer, cancel; Pi authoritative mission admission; room-to-mission
binding; session migration and fabric scheduler placement with exclusions; skill execution with audited artifacts;
Postgres lease store and migration; UI. Agents stay unable to submit tasks (decision recorded in P01 answers).

## Rollback
Delete `team_tasks.py` and its test; no schema, route or config was added.
