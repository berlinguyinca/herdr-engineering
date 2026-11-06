# Task 10 report (docs, e2e, flake hardening)

Status: DONE (not pushed, not merged).

## Changes
- docs/operations/assistant-hub.md: new "Phase 4" section (Slack manifest, token registration, owner_slack trust boundary,
  one-shot approvals, remember, questions, summaries, deploy scopes config, mode kill switch + Confirm tap, hub-verified
  preconditions, outcome check table, advisory/cwd limits, "Not verified" list); replaced the "Phase 4 adds ..." sentence;
  updated the Phase 3 deferred line and Testing notes. Keys checked against assistant.py validators.
- tests/control_plane/test_assistant_hub_phase4_e2e.py: 6 tests over real Socket Mode WebSocket, GitHub (real installation
  token exchange), Woodpecker, health listeners, real REST server, real MCP hub, real judge wire listener.
  Tap approve -> authorized once then pending; question thread (stranger ignored, owner answers); green auto-deploy ->
  authorized(grant) -> healthy Slack post -> summary "healthy"; different version serving after window -> "Deploy not live
  ... cause unknown"; red CI -> pending/deploy_preconditions + owner ask, no check opened; mode flipped to ask ->
  pending/deploy_mode, grant kept, works again at auto.
- Flake hardening (wait on stored state, not the wire): test_an_unposted_question_is_posted_after_a_restart,
  test_a_permanent_error_does_not_block..., test_socket_mode_link_ignores_environment_proxies (ack may trail the tap),
  and test_unposted_ask_survives_a_restart_and_is_posted_exactly_once (same race, failed once in the full run).
  Each looped 5x x 4 parallel under `taskset -c 0`: all pass. Phase 4 e2e 3x parallel pinned to one core: all pass.
- test_assistant_hub_preconditions.py: the 4 skipped gh_* parametrizations are now excluded from the parametrize list.

## Verification
- hub suite `tests/control_plane/test_assistant_hub_*.py`: 2557 passed, 1 skipped (pre-existing integration-style skip), 1 failed
  (the race above, fixed and re-run 20x), 5 deselected (integration marker).
- tests/review_gate, tests/ci_review, tests/test_ci*.py: 926 passed.
- ruff check on all hub tests: clean. e2e with -W error: 6 passed.
- Full `pytest -q` of the whole repo was not run (only the requested subsets).

## Concerns
- e2e takes ~30 s (each test boots a world with several listeners).
- Not run against real Slack/GitHub/Woodpecker or a real deploy; stated in the docs.
- Remote stub `compare` answers one shape for every commit, so the "different version" e2e sets compare=behind explicitly.

## Fix round 1
- e2e (test_assistant_hub_phase4_e2e.py): two new tests. Approve-and-remember with the judge gate satisfied: three owner
  decisions across sessions/tasks, the third "Approve and remember", the grant goes active (confirmed by slack:owner), the
  thread says "Remembered.", and a later request from another session/task is authorized with source `grant` and no new
  action ask in Slack. Gate-refusing counterpart: the thread says "not enough of your decisions have been labeled yet",
  the grant stays `proposed`, the ask keeps `remember=True, activation=pending`, no refusal code leaks, and after the gate
  is satisfied the next approval retries and activates it.
- Red-CI e2e now finds the ask post by the button value of the stored ask (and checks its channel), not `posts()[-1]`.
- Slack taps flake: run in suite order (all earlier hub files plus the taps file, 2044 passed) with a thread-leak plugin.
  No supervisor thread leaked before `test_runtime_survives_a_dead_endpoint...`; the only leak seen was from
  `test_stop_timeout_keeps_the_handle...` itself (its replacement runtime was left running for the fixture's stop with a 0.2 s
  stop_timeout). I could not reproduce the dead-endpoint failure in order; the process-global thread count was the fragile
  part. Both tests now count only the supervisors started by their own runtime (set difference against the threads present
  before start), without loosening the exact `== 1` checks, and the stop-timeout test now stops its runtime and waits for
  its thread to die. Taps file with -W error and the leak plugin: 28 passed, no leak.
- Docs: `mode` is read live from the hub's config on every request; there is no hot-reload entry point for the assistant
  config (fleet_config reloads only host sections), so "and reloading the config" was removed and a restart is stated.
  The rolled_back row now matches the asserted wording.
- Full hub suite after the fixes, two consecutive runs: 2560 passed, 1 skipped, 5 deselected in 625.37s (0:10:25); 2560 passed, 1 skipped, 5 deselected in 620.54s (0:10:20).
