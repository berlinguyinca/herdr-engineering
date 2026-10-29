# Phase 0 — codebase audit and integration map
**Audit before edits.** Checkout current repo and compare spec against HEAD, active open PRs, deployed revision, in-progress frontend work, and existing ADRs. Record `git rev-parse HEAD`, inventory file names and ownership, deduplicate overlapping feature branches. Build a function-level call graph of EVERY launch: CLI, dashboard, Pi, Claude, Codex, autospec/plannator, collaboration fleet, handoff, delegated children, agents on remote hosts and future external peers. Find where remote process actually starts and whether new orchestration can be bypassed.

## Verified starting anchors (GitHub default branch inspected 2026-10-25)
* `deploy/control-plane/collab-spawn.py`: `Runner.discover`, `reachable`, `installed_families`, `pick_host`, `herd_pane`, `place`. The current fleet runner uses round-robin and has a `parent_host or self.pick_host(eligible)` path. Replace implicit host inheritance; preserve explicit authorized pinning and parent-linked visualization even across hosts.
* `herdr_engineering/herdr_adapter.py`: `NativeHerdrAdapter` and `Machine` implement machine discovery (`herdr machine list --json`), snapshots and `--machine` forwarding. Extend, don't fork.
* `herdr_engineering/control_plane/collab.py`: persisted collaboration events and `fleet` settings; existing `auto_agents` flags/host registration and authentication boundaries. Reuse; migrate without losing intent.
* `herdr_engineering/fabric.py`: existing per-core host-load logic; reuse or refactor after examining existing telemetry API and freshness.
* `docs/operations/collaboration-network-guide.md`: documents automatic agents toggles, host reachability and fleet spawn semantics.
* `docs/operations/agent-summary-fabric.md`: documents inference scheduling ownership boundary: InferWeave owns worker selection/model placement.
These anchors are **discovery clues, not evidence that all spawn integrations are covered**. Audit actual current code and deployment.

## Required artifacts before code
1. `docs/specs/<unique>/audit.md`: current-state route/component/schema map with concrete file paths and symbols, reusable modules, changes needed and no-dup rationale.
2. `docs/specs/<unique>/launch-path-matrix.md`: launch source → decision point → remote launcher → identity linkage → policy enforcement. Every row has test ID.
3. Current dashboard UX/component architecture and route names, including work-in-progress areas.
4. Migration compatibility assessment for flags, store, host aliasing, cluster version, local-only and remote flows.
5. Identified competing PR/spec overlap and merge strategy. Do not overwrite colleagues' concurrent work.

## Exit gate
All launch paths enumerated, integration seams proven by links to code, tests runnable, no unreviewed parallel scheduler introduced.
