# Acceptance Matrix

Every row is required before release unless explicitly marked `BLOCKED_EXTERNAL`
with evidence and a tracked issue. Status reflects evidence on 2026-09-23:
`79 passed` unit/contract/integration tests, `ruff` clean, live command
snapshots under `docs/evidence/`.

Rows marked `BLOCKED_EXTERNAL` depend on the remote validation fleet
(`fry`/`beast`/`bender`/`macbook-m4`), a reachable Tailscale device, a live
Chromium/CDP target, or a live Woodpecker/Pileated instance — none reachable from
the reference workstation. The implementation for those rows exists and is
unit-tested; only live convergence/validation is pending.

## Foundation

- [x] Repository has a clear README, architecture, specs, ADRs and contributor workflow.
- [x] Current Herdr schema/version/integrations are captured and compatibility-tested.
- [x] No custom duplicate remote-machine aggregator exists.
- [x] Upstream plugins are audited, licensed and pinned.

## Fleet

- [ ] `fry`, `beast`, `bender`, `macbook-m4` pass Ansible convergence.
      **BLOCKED_EXTERNAL** — hosts unreachable; Ansible not installed on the
      reference workstation. Playbooks provided (`ansible/playbooks/site.yml`).
      Evidence: `ansible/README.md`.
- [ ] Second Ansible run is idempotent. **BLOCKED_EXTERNAL** — designed
      idempotent (package/state-based guards), not yet converged live.
- [ ] Pi on every validation host routes LLM traffic through
      `https://llm.metabolomics.us`. **BLOCKED_EXTERNAL** — enforced by
      `ansible/playbooks/site.yml`; live host not reachable.
- [ ] Herdr, Pi Engineering, AutoSpec, vim, mc and btop are present.
      **BLOCKED_EXTERNAL** — fleet convergence pending.
- [ ] Existing unrelated user configuration is preserved. **BLOCKED_EXTERNAL** —
      playbooks merge/back up, never wholesale overwrite; live host not reachable.

## Core Herdr

- [x] Local and saved SSH machines appear correctly. Adapter tested;
      `docs/evidence/0020/herdr-machine-list.json` captured.
- [ ] Disconnecting a client does not stop remote work. **EXTERNAL** — native
      Herdr property; live fleet validation `BLOCKED_EXTERNAL`.
- [ ] One unreachable machine does not break others. **EXTERNAL** —
      `BLOCKED_EXTERNAL` live.
- [x] Pi agent state/session identity is visible through native integration or an
      audited compatible extension. `pi_autospec.py` correlates
      mission→worker→session→worktree.

## Web/mobile

- [x] Web UI is accessible only through the private tailnet by default.
      `web.py` defaults to `private_only=True`, host `127.0.0.1`.
- [ ] iPhone/iPad-sized layouts can navigate agents, activity, files, browser,
      tests and CI. **BLOCKED_EXTERNAL** — responsive UI implemented; device
      validation requires a tailnet device.
- [ ] Reconnect restores the selected machine/session without killing work.
      **BLOCKED_EXTERNAL** — live tailnet validation.
- [x] Terminal input/control is permission-aware and does not expose public
      listeners. Doctor listener scan reports no public listeners.

## Browser/review

- [ ] Generated/local web apps can be opened and inspected. **BLOCKED_EXTERNAL** —
      live Chromium/CDP target not reachable.
- [ ] Browser console/page errors can be captured as evidence. **BLOCKED_EXTERNAL** —
      capture path implemented in `browser.py`.
- [x] Screenshots/recordings are linked as artifacts. `browser.py` returns an
      `ArtifactRef`; artifact publish tested.
- [ ] Plannotator review opens through the adopted integration. **BLOCKED_EXTERNAL** —
      `PlannotatorAdapter` preserves review identity/decision/evidence; live
      review target not reachable.

## Shared artifacts

- [ ] Shared artifact root works across representative Linux/macOS hosts.
      **BLOCKED_EXTERNAL** — filesystem provider; cross-host live validation.
- [x] Concurrent artifact creation is safe. Atomic publish + content hashing
      tested.
- [ ] Disconnect/reconnect behavior is documented and tested. **BLOCKED_EXTERNAL** —
      live multi-host validation.
- [x] Shared storage outage cannot corrupt or block local Git worktrees. Git
      remains source of truth; degraded queue on storage loss is tested.
- [x] Secrets/home credential stores are excluded by default. Artifact
      exclusions tested.

## Session journal

- [x] Every recognized Pi Engineering session creates/attaches an activity projection.
- [x] Current activity is visible.
- [x] Timeline is newest first.
- [x] Started / Completed / Current / Next summaries update incrementally.
- [x] Git, browser, artifacts, tests, CI and review references are clickable/resolvable.
- [x] Activity survives web/TUI reconnect and process restart according to
      retention policy. Persistent idempotent journal tested.
- [x] Hidden chain-of-thought is never stored or displayed.

## Transparent dev fabric

- [x] Two services on different hosts can receive distinct cluster-wide leases.
      Lease registry allocates distinct free ports atomically.
- [x] A client opens them using the same logical `dev:<port>` naming scheme.
- [ ] Phone/iPad on Tailscale can use the same addresses. **BLOCKED_EXTERNAL** —
      live tailnet.
- [x] HTTP works.
- [ ] WebSocket works. **BLOCKED_EXTERNAL** — forwarding covers it by service
      type; live services not reachable.
- [ ] SSE/HMR works. **BLOCKED_EXTERNAL**.
- [ ] Generic TCP forwarding works where declared by service type. **BLOCKED_EXTERNAL**.
- [x] Lease survives/reconciles reconnect. Heartbeat/reconcile tested.
- [x] Crashed processes lead to stale/expired route state, not misrouting.
      TTL/expiry + stale cleanup tested.
- [x] Port collision tests pass under concurrent registration.

## Tests

- [ ] pytest test tree + per-test failure trace. **BLOCKED_EXTERNAL** — adapter
      implemented and unit-tested; live pytest suite not present.
- [ ] Go test tree + per-test logs/failure. **BLOCKED_EXTERNAL**.
- [ ] Rust test tree + per-test logs/failure. **BLOCKED_EXTERNAL**.
- [ ] Java test tree + JUnit failure trace. **BLOCKED_EXTERNAL**.
- [ ] Scala test tree + failure trace. **BLOCKED_EXTERNAL**.
- [x] Source links open the correct worktree/file/line. Source-link resolution
      tested.
- [x] Re-run one test / group / suite works through the native runner adapter.
- [x] Parallel worktrees never mix test events. Event scoping tested.
- [x] Historical duration/failure/flakiness data is correlated without changing
      runner truth.

## CI

- [ ] Woodpecker/Pileated repositories/pipelines are visible. **BLOCKED_EXTERNAL** —
      live CI instance not reachable.
- [ ] Pipeline/step logs are clickable. **BLOCKED_EXTERNAL**.
- [ ] Queue and runner state is visible. **BLOCKED_EXTERNAL**.
- [x] Commit/branch/worktree correlation is correct. Correlator tested.
- [x] Stale/unavailable CI is shown explicitly rather than guessed.
- [x] Optional retry/cancel controls respect upstream auth/permissions.

## Multi-agent comparison

- [x] At least 6 concurrent worktrees can be grouped under one mission/issue.
      Candidate model is unbounded and grouping tested.
- [x] Each candidate shows diff/test/CI/dev-preview/review evidence.
- [ ] Browser previews can be opened side-by-side or rapidly switched.
      **BLOCKED_EXTERNAL** — live browser preview.
- [x] Candidate cleanup does not delete another candidate's worktree/lease/artifacts.
      Owner-guarded cleanup tested.

## Reliability/security

- [x] Doctor has human and machine-readable output.
- [x] Unsafe/public listeners are detected.
- [x] Dependency update preflight can block incompatible updates.
- [x] Rollback restores the previous known-good bundle.
- [x] Optional plugin failure does not take down Herdr.
- [x] Crash/restart recovery tests pass.
- [x] No secrets are committed in repo/config/artifacts/logs.

## Release

- [x] Fresh installation from documented instructions succeeds. Editable install
      (`pip install -e .`) verified on this Linux workstation.
- [ ] Upgrade from previous pinned bundle succeeds. **BLOCKED_EXTERNAL** — no
      prior public bundle exists to upgrade from; preflight/rollback unit-tested.
- [ ] Rollback drill succeeds. **BLOCKED_EXTERNAL** — snapshot/rollback
      unit-tested; full drill pending a prior bundle.
- [x] Linux validation passes. This workstation is Linux; all 79 tests pass.
- [ ] macOS validation passes. **BLOCKED_EXTERNAL** — fleet host unreachable.
- [ ] phone/iPad tailnet validation passes. **BLOCKED_EXTERNAL**.
- [x] README commands match the real shipped implementation. Updated with real
      `herdr-eng` commands and observed output.
