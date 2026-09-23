# Acceptance Matrix

Every row is required before release unless explicitly marked `BLOCKED_EXTERNAL` with evidence and a tracked issue.

## Foundation

- [ ] Repository has a clear README, architecture, specs, ADRs and contributor workflow.
- [ ] Current Herdr schema/version/integrations are captured and compatibility-tested.
- [ ] No custom duplicate remote-machine aggregator exists.
- [ ] Upstream plugins are audited, licensed and pinned.

## Fleet

- [ ] `fry`, `beast`, `bender`, `macbook-m4` pass Ansible convergence.
- [ ] Second Ansible run is idempotent.
- [ ] Pi on every validation host routes LLM traffic through `https://llm.metabolomics.us`.
- [ ] Herdr, Pi Engineering, AutoSpec, vim, mc and btop are present.
- [ ] Existing unrelated user configuration is preserved.

## Core Herdr

- [ ] Local and saved SSH machines appear correctly.
- [ ] Disconnecting a client does not stop remote work.
- [ ] One unreachable machine does not break others.
- [ ] Pi agent state/session identity is visible through native integration or an audited compatible extension.

## Web/mobile

- [ ] Web UI is accessible only through the private tailnet by default.
- [ ] iPhone/iPad-sized layouts can navigate agents, activity, files, browser, tests and CI.
- [ ] Reconnect restores the selected machine/session without killing work.
- [ ] Terminal input/control is permission-aware and does not expose public listeners.

## Browser/review

- [ ] Generated/local web apps can be opened and inspected.
- [ ] Browser console/page errors can be captured as evidence.
- [ ] Screenshots/recordings are linked as artifacts.
- [ ] Plannotator review opens through the adopted integration.

## Shared artifacts

- [ ] Shared artifact root works across representative Linux/macOS hosts.
- [ ] Concurrent artifact creation is safe.
- [ ] Disconnect/reconnect behavior is documented and tested.
- [ ] Shared storage outage cannot corrupt or block local Git worktrees.
- [ ] Secrets/home credential stores are excluded by default.

## Session journal

- [ ] Every recognized Pi Engineering session creates/attaches an activity projection.
- [ ] Current activity is visible.
- [ ] Timeline is newest first.
- [ ] Started / Completed / Current / Next summaries update incrementally.
- [ ] Git, browser, artifacts, tests, CI and review references are clickable/resolvable.
- [ ] Activity survives web/TUI reconnect and process restart according to retention policy.
- [ ] Hidden chain-of-thought is never stored or displayed.

## Transparent dev fabric

- [ ] Two services on different hosts can receive distinct cluster-wide leases.
- [ ] A client opens them using the same logical `dev:<port>` naming scheme.
- [ ] Phone/iPad on Tailscale can use the same addresses.
- [ ] HTTP works.
- [ ] WebSocket works.
- [ ] SSE/HMR works.
- [ ] Generic TCP forwarding works where declared by service type.
- [ ] Lease survives/reconciles reconnect.
- [ ] Crashed processes lead to stale/expired route state, not misrouting.
- [ ] Port collision tests pass under concurrent registration.

## Tests

- [ ] pytest test tree + per-test failure trace.
- [ ] Go test tree + per-test logs/failure.
- [ ] Rust test tree + per-test logs/failure.
- [ ] Java test tree + JUnit failure trace.
- [ ] Scala test tree + failure trace.
- [ ] Source links open the correct worktree/file/line.
- [ ] Re-run one test / group / suite works through the native runner adapter.
- [ ] Parallel worktrees never mix test events.
- [ ] Historical duration/failure/flakiness data is correlated without changing runner truth.

## CI

- [ ] Woodpecker/Pileated repositories/pipelines are visible.
- [ ] Pipeline/step logs are clickable.
- [ ] Queue and runner state is visible.
- [ ] Commit/branch/worktree correlation is correct.
- [ ] Stale/unavailable CI is shown explicitly rather than guessed.
- [ ] Optional retry/cancel controls respect upstream auth/permissions.

## Multi-agent comparison

- [ ] At least 6 concurrent worktrees can be grouped under one mission/issue.
- [ ] Each candidate shows diff/test/CI/dev-preview/review evidence.
- [ ] Browser previews can be opened side-by-side or rapidly switched.
- [ ] Candidate cleanup does not delete another candidate's worktree/lease/artifacts.

## Reliability/security

- [ ] Doctor has human and machine-readable output.
- [ ] Unsafe/public listeners are detected.
- [ ] Dependency update preflight can block incompatible updates.
- [ ] Rollback restores the previous known-good bundle.
- [ ] Optional plugin failure does not take down Herdr.
- [ ] Crash/restart recovery tests pass.
- [ ] No secrets are committed in repo/config/artifacts/logs.

## Release

- [ ] Fresh installation from documented instructions succeeds.
- [ ] Upgrade from previous pinned bundle succeeds.
- [ ] Rollback drill succeeds.
- [ ] Linux validation passes.
- [ ] macOS validation passes.
- [ ] phone/iPad tailnet validation passes.
- [ ] README commands match the real shipped implementation.
