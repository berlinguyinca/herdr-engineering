# Pi session recovery implementation plan

Goal: recover eligible outage-stalled sessions through native HerdR controls with a minimum 60-second cooldown after every attempted session.

Architecture: reuse fleet rollout native discovery, foreground proof and typed mission maintenance. A private durable journal and controller-wide flock prevent duplicate/overlapping recovery. A bounded same-user metadata reader establishes process birth, transcript failure and model requirements; actual tool-call probes gate model selection.

Scope: herdr_engineering/pi_recovery.py, pi_recovery_host.py, cli.py, tests/test_pi_recovery.py, skills/pi-recovery/SKILL.md, deploy/herdr-pi-recovery-plugin/herdr-plugin.toml. No service restart or transport bypass.

- [x] Write failing tests for exclusions, identity races, ambiguous prompts, duplicate plans, crash pacing and lock contention.
- [x] Implement read-only plan/status and explicit apply with durable attempted marker, completion cooldown, fresh proofs, exact model acknowledgement, typed mission fences, and bounded reports.
- [x] Add CLI, read-only menu action and operating skill.
- [x] Run focused tests, regression tests, lint, compilation and independent review; commit with Lore context.

Review-driven refinements: exclude unresolved/error-turn tool actions and ambiguous transcript branches; require visible empty editor and no busy indicator; use SDK reload preserving launch flags instead of unprovable process restart; negotiate typed exact-model mission resume plus runtime idleness attestation. No deployment or live prompts belong to this implementation lane.
