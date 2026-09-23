# HerdR Engineering — Complete Master Specification

**Canonical repository:** `berlinguyinca/herdr-engineering`  
**Generated:** 2026-09-23  
**Purpose:** single-file concatenation of every current normative implementation spec, in dependency/implementation order. The decomposed files under `docs/specs/` remain the easiest units for implementation and review.

See `docs/spec-genealogy.md` for historical chronology and `SPEC_MANIFEST.yaml` for machine-readable dependencies.

---

# 0000 — Master Program: Super HerdR / HerdR Engineering

## Objective

Build `berlinguyinca/herdr-engineering` from an empty repository into a reproducible, private, cross-machine engineering cockpit around Herdr. The result should let one operator supervise dozens of persistent Pi/engineering sessions and worktrees across multiple machines from desktop, phone or tablet, while keeping source ownership, model routing, CI and review in their proper systems.

This master spec consolidates the earlier Herdr runtime-integration, distributed-fleet, Powerpack/plugin, shared-workspace/session-panel, transparent-dev-fabric and testing/CI specifications into one dependency-gated program.

## Product boundary

HerdR Engineering owns:

- curated/audited dependency and plugin distribution;
- declarative fleet installation/configuration;
- fleet-wide projections/navigation built on native Herdr machine primitives;
- web/mobile integration;
- browser/preview/review integration;
- shared artifact/handoff space;
- normalized session activity/timeline projection;
- transparent ephemeral dev-service addressing;
- normalized test explorer and CI views;
- candidate/worktree comparison UX;
- health, security, observability, notification, update and rollback integration.

It does not own:

- persistent terminal/runtime implementation — Herdr;
- agent mission/DAG/review orchestration — Pi Engineering;
- specs/issues workflow — AutoSpec;
- inference/GPU scheduling — Tern;
- CI execution — Woodpecker/Pileated;
- review semantics — Plannotator;
- Git truth — Git/GitHub;
- Pi-specific UI implementation — Pi-Web.

## User journeys that must work at release

### 1. Start remote work and leave

An operator launches Pi work on `bender`, closes the laptop, later opens HerdR Web on an iPad and sees the same live session, current state, activity summary and worktree. The remote process has continued uninterrupted.

### 2. Dozens of UI candidates

Pi Engineering fans one mission into isolated worktrees across several hosts. Each candidate launches its existing random-port React/Next/Vite server. HerdR Engineering assigns a fleet-wide lease and exposes each preview through `dev:<port>`, then groups diff/test/CI/review/browser evidence so candidates can be compared without remembering hosts.

### 3. Debug a failed test

The Tests view shows repository → suite/class/module → test hierarchy. A failed test opens its trace, log artifacts and exact worktree source line. The operator reruns the single test using the native framework command and can hand the failure context to the owning Pi session.

### 4. Understand CI pressure

The CI view shows Woodpecker/Pileated pipeline state, steps, queue and runners. A failed pipeline links back to the Git revision, related session/activity and test evidence. Supported retry/cancel actions are permission-gated and executed by the CI provider.

### 5. Know what every agent is doing

Each session has a bounded newest-first timeline and Started / Completed / Current / Next projection. Evidence is clickable. No hidden reasoning is captured.

## Global invariants

1. Never build a second source of truth for a concern already owned by another system.
2. Never silently fall back to terminal scraping if a structured source exists.
3. Never expose internal development/debug services publicly by default.
4. Never persist secrets or hidden chain-of-thought.
5. Never make shared storage a multi-writer source checkout.
6. Never hard-code the validation host list into application behavior.
7. Keep external dependencies replaceable behind HerdR Engineering contracts/adapters.
8. All externally sourced plugins/dependencies are pinned after compatibility/security/license audit.
9. Any control action is authenticated, authorized, auditable and idempotent where practical.
10. Failure of one optional plugin or one remote host must not take down the rest of Herdr.

## Program sequence

| Phase | Outcome |
|---|---|
| 0010 | repository/reconciliation foundation |
| 0020 | verified upstream compatibility matrix |
| 0030 | stable internal contracts over native Herdr |
| 0040 | declarative four-host fleet bootstrap |
| 0050 | pinned Powerpack distribution/lifecycle |
| 0060 | private web/mobile control surface |
| 0070 | browser preview + Plannotator integration |
| 0080 | shared artifact/handoff workspace |
| 0090 | session journal/timeline projection |
| 0100 | semantic Pi Engineering/AutoSpec correlation |
| 0110 | transparent `dev:<port>` fabric |
| 0120 | normalized cross-language Tests explorer |
| 0130 | Woodpecker/Pileated CI view |
| 0140 | multi-agent worktree/candidate comparison |
| 0150 | coherent unified engineering UX |
| 0160 | attention/notifications/review actions |
| 0170 | observability/security/update/rollback |
| 0180 | full fleet validation, docs and release |

## Completion rule

Do not declare the project complete until `docs/acceptance-matrix.md` is fully evidenced. A genuine external blocker may be marked `BLOCKED_EXTERNAL`, but must include evidence, affected acceptance rows, a tracked issue and a clear safe degraded behavior.

---

# 0010 — Repository Bootstrap and Reconciliation

## Goal

Create the canonical repository structure and establish what is already implemented elsewhere before writing product code.

## Requirements

- Initialize or reconcile `berlinguyinca/herdr-engineering`; preserve existing Git history if present.
- Copy this complete spec bundle into the repository.
- Establish formatting/lint/test/build conventions appropriate to the chosen implementation languages after repository inspection; do not choose a language merely because the spec is Markdown.
- Create `IMPLEMENTATION_RECONCILIATION.md` mapping every requirement to `EXISTING`, `PARTIAL`, `MISSING`, `CONFLICTING`, `EXTERNAL`, or `SUPERSEDED` with source links.
- Inspect relevant local/remote repositories: Herdr integration points, Pi Engineering, AutoSpec, fleet Ansible, Pileated/Woodpecker integration and any pre-existing HerdR add-ons.
- Record which repository owns each required code change.
- Preserve archived predecessor specs and record conflicts against current normative specs.
- Create basic GitHub issue labels/milestones or equivalent phase mapping so the implementation program is visible in GitHub.

## Deliverables

- `IMPLEMENTATION_RECONCILIATION.md`
- initial repository CI for docs/schema/config validation
- phase/milestone issue structure
- updated `STATUS.md`

## Exit gate

A reviewer can trace every master requirement to a current owner/status, and no implementation phase relies on an unidentified repository or guessed pre-existing API.

---

# 0020 — Upstream Audit and Compatibility

## Goal

Replace assumptions with a tested compatibility matrix against the actual installed/current Herdr ecosystem before dependency lock-in.

## Required discovery

Capture and retain machine-readable output where supported for:

```text
herdr --version
herdr api schema --json
herdr machine list --json
herdr plugin list --json
herdr integration status
```

Inspect official Herdr documentation/source for machine connectivity, socket/plugin API, worktrees, events/config and Pi integration.

Audit candidate dependencies from `docs/upstream-baseline-2026-09-23.md`, including newer alternatives discovered during implementation.

## Classification

For every candidate record:

- canonical repository + immutable tested revision;
- license;
- maintenance/release state;
- Herdr version/API compatibility;
- Linux/macOS support;
- install/build scripts and supply-chain risk;
- listeners/network exposure;
- credential needs;
- whether native Herdr now supersedes it;
- decision: `ADOPT`, `ADAPT`, `OPTIONAL`, `HOLD`, `REJECT`.

## Mandatory architecture validation

Explicitly verify that native Herdr saved machines and machine-targeted operations are sufficient for the base fleet transport. A custom replacement requires a new ADR proving the gap.

## Deliverables

- `HERDR_COMPATIBILITY.md`
- populated `lock/upstreams.yaml`
- compatibility tests for adopted plugins
- ADR updates for changed assumptions

## Exit gate

No dependency enters the Powerpack without a tested revision and classification, and the implementation uses native Herdr primitives wherever they satisfy the requirement.

---

# 0030 — Native Herdr Contracts and Adapter Layer

## Goal

Create a small HerdR Engineering adapter boundary over the actual Herdr API so higher phases do not depend on brittle terminal parsing or scattered CLI calls.

## Required capabilities

Normalize, without duplicating Herdr state:

- list/resolve machines and connectivity state;
- list/get workspaces, tabs, panes and sessions;
- agent integration state and observable lifecycle fields;
- send permitted terminal/session input;
- create/list/remove worktrees using native capability where appropriate;
- plugin capability discovery;
- event subscription/streaming when available;
- version/schema/capability negotiation;
- machine-targeted invocation;
- structured errors (`unreachable`, `permission`, `unsupported`, `timeout`, `conflict`, `invalid`).

## Design

- Treat Herdr session/worktree IDs as foreign/native IDs; do not mint competing canonical IDs unnecessarily.
- Expose capabilities explicitly so UI/features can degrade rather than assume all versions/plugins support all actions.
- Prefer direct socket/plugin APIs. CLI wrappers are acceptable only behind the adapter and must use structured output when supported.
- Correlate remote errors to the target machine; one failed host never changes another host to unknown.
- Add contract tests using representative local and saved-SSH machine fixtures/mocks plus at least one real smoke test.

## Exit gate

A thin test application can enumerate the fleet, sessions/worktrees and Pi integration state through one typed interface, target a specific saved machine, survive one unreachable machine, and contain no terminal-screen scraping for these capabilities.

---

# 0040 — Fleet Bootstrap and Ansible

## Goal

Make a supported machine reproducibly ready for the HerdR Engineering ecosystem without ad-hoc SSH configuration.

## Validation inventory

Initial names: `fry`, `beast`, `bender`, `macbook-m4`. Treat these as inventory examples, not product constants.

## Managed baseline

Declaratively ensure, preserving user config:

- Herdr at the tested/pinned compatible version;
- Pi;
- Pi provider configuration pointing all lab LLM work at `https://llm.metabolomics.us`;
- `berlinguyinca/pi-engineering`;
- `berlinguyinca/autospec`;
- Tailscale presence/connectivity checks (do not embed reusable auth keys in Git);
- `vim`, `mc`, `btop`;
- prerequisites for adopted Powerpack components;
- directories/permissions for HerdR Engineering state and artifact cache.

## Platform behavior

- Linux and macOS roles may differ internally but expose the same resulting capabilities.
- Existing Herdr/Pi/Tailscale config must be merged/backed up, not overwritten wholesale.
- No secrets in inventory or repository.
- `--check`/dry-run support where practical.
- Second converged run must be idempotent.

## Exit gate

All four validation hosts converge, a second run produces no unexpected changes, and a new example host can be added through inventory alone.

---

# 0050 — Powerpack Distribution and Lifecycle

## Goal

Provide one supported installation profile that pins and safely configures the adopted Herdr extensions/integrations without turning them into a monolithic fork.

## Powerpack responsibilities

- dependency manifest/lock;
- platform-aware installation;
- safe config merge and backup;
- capability/health checks;
- plugin enable/disable policy;
- version compatibility enforcement;
- update preflight;
- rollback to prior known-good lock/config;
- machine-readable `doctor` output.

## Candidate capabilities

The audit may adopt or adapt browser integration, herdr-web, Plannotator integration, swarm/worktree helpers, file annotation, GitHub checks/PR board and notifications. Do not require every candidate to be installed if native Herdr or a better upstream makes it redundant.

## Configuration precedence

Define explicit precedence:

```text
shipped safe defaults
< fleet/site config
< user config
< repository config
< session override
```

Merging must preserve unrelated user settings.

## Failure isolation

- Optional plugin startup failure degrades that capability, not core Herdr.
- Version mismatch is detected before destructive update.
- A rollback command restores dependency lock + managed config snapshot.

## Exit gate

One documented installation/update command produces the same compatible Powerpack on Linux/macOS validation hosts, `doctor --json` accurately reports capability state, and rollback from a deliberately incompatible test revision succeeds.

---

# 0060 — Web and Mobile Control Surface

## Goal

Make the HerdR cockpit usable from desktop browsers, iPhone and iPad over the private tailnet without replacing the native terminal UI.

## Approach

Prefer the audited `herdr-web` upstream if compatible. Extend/adapt through supported APIs rather than rebuilding all Herdr views.

## Required UX

Responsive navigation must expose at minimum:

- machines/connectivity;
- sessions/agents and status;
- session attach/input where authorized;
- activity/timeline placeholder surface for phase 0090;
- worktree/repository context;
- files/artifacts;
- browser/preview entry point;
- Tests and CI navigation placeholders for later phases;
- attention/approval affordances when those phases land.

Mobile must prioritize status/steering/review over trying to be a full desktop IDE.

## Networking/security

- Bind/private-route so the UI is tailnet/private by default.
- Reuse Tailscale identity/network boundary where suitable; add application authorization for sensitive actions.
- No public fallback listener.
- Reconnect restores view/session selection without restarting work.
- Terminal input is explicit and auditable.

## Exit gate

From an iPhone/iPad-sized browser on Tailscale, the operator can navigate all validation machines, inspect a running Pi session, reconnect after network interruption and send an authorized input without exposing the service publicly.

---

# 0070 — Browser Preview and Plannotator

## Goal

Bring generated web applications, browser diagnostics and human review directly into the HerdR workflow.

## Browser integration

Prefer an audited upstream Herdr Browser implementation. Required capabilities:

- open URLs tied to session/worktree/dev service;
- Chromium/CDP-based page inspection when supported;
- screenshot capture as `ArtifactRef`;
- browser console/page error capture as bounded evidence;
- viewport presets for desktop/tablet/phone;
- safe Playwright/CDP automation hooks for test/evidence workflows;
- private debug endpoint handling; never expose CDP publicly.

## Plannotator

Prefer the upstream Herdr-Plannotator/annotation integration where compatible. A review should open from the related session/activity/candidate and preserve review identity, comments, decision and evidence links.

## Artifact integration

Browser screenshots, page captures and review exports use `ArtifactRef` and attach to session/candidate/test records rather than being hidden in plugin-local directories.

## Exit gate

A worktree web app can be opened in the HerdR browser view, console errors and screenshots can be attached as evidence, phone/tablet viewport can be inspected, and a Plannotator review can be initiated/resolved through the adopted integration.

---

# 0080 — Shared Artifact and Handoff Workspace

## Goal

Give agents and humans a convenient common place for outputs while preserving local Git worktree isolation.

## Scope

Support generated artifacts such as:

- screenshots/video captures;
- plans/spec extracts;
- reports/plots;
- test/coverage exports;
- browser captures;
- review handoffs;
- packaged logs;
- candidate comparison bundles.

## Architecture

Implement an abstract artifact workspace provider first. The initial concrete provider may be a shared filesystem available to the fleet, but the product contract must not depend on one mount technology.

Recommended logical hierarchy:

```text
<root>/<project>/<mission-or-session>/<artifact-id>/...
```

Use metadata/provenance, content hashes and `ArtifactRef`; do not make human-readable path layout the only index.

## Safety

- Never sync `.git`, SSH keys, cloud credentials, auth tokens, browser profiles or arbitrary home directories by default.
- Use atomic publish/rename patterns for completed files.
- Handle concurrent writers without last-writer corruption.
- Local work continues if shared storage is down; publishing becomes queued/degraded with visible status.
- Retention/quota policy is configurable.

## Exit gate

Linux/macOS hosts can publish/read artifacts concurrently, provenance resolves back to owner session/worktree, simulated storage loss cannot corrupt active repositories, and reconnection safely resumes or reports failed publication.

---

# 0090 — Session Journal, Client Panel and Timeline

## Goal

For every HerdR/Pi engineering session, automatically show what it started to do, what it completed, what it is doing now and what explicit next step is known, plus a newest-first evidence timeline.

## Data model

Use `docs/contracts/activity-event.md`. This is a derived activity projection, not a competing Pi Engineering event bus/task state machine.

## Sources

Ingest structured observable facts from:

- native Herdr session/agent lifecycle;
- Pi/Pi Engineering semantic lifecycle events;
- AutoSpec issue/spec transitions;
- Git changes/commits;
- artifact publication;
- browser/dev-service events;
- tests;
- CI;
- Plannotator review;
- explicit human notes/actions.

Adapters should be independently retryable and idempotent.

## Session discovery and search

Provide a recent-session view scoped by repository/directory as well as global search. Operators should be able to find sessions without copy/pasting old session IDs or prompts. Index at least repository, worktree, branch, machine, session start/end time, current status, explicit mission/issue identity, and bounded summary text. Selecting a historical session opens its retained timeline/evidence and, when still live, offers native reattach.

This is a projection over native Herdr/Pi session identity, not a second session runtime.

## Client panel

Each session gets four primary views:

1. **CURRENT** — current observable state, task, repo/worktree/machine, blocking/attention state.
2. **TIMELINE** — newest first; filterable by source/type/severity.
3. **SUMMARY** — Started / Completed / Current / Next, incrementally maintained from explicit structured facts.
4. **RELATED** — artifacts, dev previews, tests, CI, PR/review, mission/issue, sibling candidates.

Do not expose hidden reasoning. Large logs are artifacts, not giant timeline events.

## Persistence

Persist enough normalized projection/event metadata to survive client and service restart. If an owning system can replay canonical events, store correlation/checkpoint metadata and avoid needless duplication of large payloads.

## Exit gate

A Pi session can disconnect/reconnect/restart its UI and retain correct timeline/current summary; duplicate/out-of-order events do not corrupt state; evidence links resolve; no chain-of-thought-like content is persisted by the journal.

---

# 0100 — Pi Engineering and AutoSpec Semantic Integration

## Goal

Make HerdR Engineering understand the engineering workflow semantically without taking control of it.

## Ownership

Pi Engineering remains mission/worker/DAG/policy/review/repair authority. AutoSpec remains spec/issue workflow authority. HerdR Engineering consumes typed lifecycle/state and offers navigation/actions back through documented interfaces.

## Pi Engineering integration requirements

Honor the staged Herdr runtime migration design already specified in Pi Engineering:

- Herdr is an external runtime dependency;
- Pi Engineering accesses runtime concerns behind its `AgentRuntime` abstraction;
- normalized worker states/events and structured `WorkerResult` are emitted;
- worktree ownership is explicit;
- artifact-first bounded communication prevents oversized 413-style request bodies;
- dynamic model/context metadata comes from Tern/provider capabilities rather than fixed context constants;
- legacy runtime stays available until parity/recovery/canary/rollback gates pass.

HerdR Engineering should consume these semantic events and expose deep links to the owning Pi session/worktree/artifacts.

## AutoSpec correlation

Correlate spec → issue → mission → worker/session → worktree → commit/PR/test/CI/review through stable identifiers. Do not infer issue ownership from branch names when an explicit identifier exists.

## Cross-repository changes

If Pi Engineering/AutoSpec lacks a needed event/endpoint, add the smallest compatible contract in that owning repository and pin the required version. Do not reproduce their orchestration logic inside HerdR Engineering.

## Exit gate

A real AutoSpec/Pi Engineering mission with multiple workers is represented correctly in HerdR: workers map to their native sessions/worktrees; mission/issue status comes from the owner; restarts/recovery do not create duplicate workers; and runtime migration/canary state remains visible but controlled by Pi Engineering.

---

# 0110 — Transparent Development Service Fabric

## Goal

Let dozens of agent worktrees launch web/dev servers on any participating machine and make each reachable from every tailnet device using one logical `dev:<leased-port>` address, without human-managed hostnames.

## Existing launch behavior

Do not replace the user's existing skill/process that chooses a random free local port and launches the app. HerdR Engineering registers/discovers that resulting local endpoint and adds the fleet-wide reachability layer.

## Components

1. **Lease registry** implementing `DevServiceLease`.
2. **Host registrar/agent** that verifies target process/listener ownership and renews health.
3. **Private router/service front door** associated with the stable Tailscale `dev` service name.
4. **Herdr UI integration** showing preview address, owner session/worktree and health.
5. **Cleanup/reconciler** for process exit, machine loss, service restart and orphaned leases.

## Allocation

- Use an administratively configurable external port range.
- Allocation must be atomic under concurrent requests across hosts.
- Registration is idempotent for the same process/session identity.
- Do not reassign an active or uncertain lease to another worktree.
- TTL + heartbeat + explicit close; crash reconciliation prevents permanent leaks.

## Protocol correctness

Validate:

- HTTP/1.1 and normal browser navigation;
- HTTPS when enabled;
- WebSocket upgrades;
- SSE;
- Vite/Next/React hot-module reload patterns;
- cookies/headers/path/query preservation;
- streaming/chunked responses;
- declared generic TCP pass-through.

## Security

- Tailnet/private exposure only by default.
- Route only to registered target machine/loopback endpoint.
- Reject arbitrary user-supplied target hosts outside policy.
- Audit lease create/renew/close and access-control decisions.
- Optional project/user policy can restrict who may open/control a service.

## Exit gate

A stress test concurrently registers services on at least three hosts, allocates collision-free ports, reaches each from desktop and mobile through `dev:<port>`, passes WebSocket/SSE/HMR tests, then kills processes/hosts and demonstrates correct stale-route cleanup with no cross-routing.

---

# 0120 — Unified Test Protocol and Explorer

## Goal

Provide the familiar IDE test-tree experience across machines/worktrees and languages while executing the project's native test tools.

## UX

The Tests surface must support:

- tree hierarchy with running/passed/failed/skipped/error states;
- aggregate counts and elapsed time;
- click a node for stdout/stderr/logs, assertion message and stack/trace;
- exact source-file/line link in the owning worktree;
- rerun one test, group/suite or entire run;
- cancel when native runner supports it;
- filters for failed/running/flaky/recent;
- historical duration/failure signals;
- correlation to session/mission/commit/CI.

## Adapter architecture

Implement a runner adapter interface that emits `TestEvent`. Required release adapters:

- Python: pytest;
- Go: `go test` structured/JUnit-compatible output as appropriate;
- Rust: Cargo test / compatible structured reporter;
- Java: JUnit reports/framework-native events;
- Scala: sbt/ScalaTest/JUnit-compatible evidence as appropriate.

Do not make all projects adopt one wrapper just to appear in the UI. Where runners have no ideal live protocol, combine process progress + completed standard report import.

## Logs and artifacts

Bound streamed log data. Persist large traces/logs/reports as artifacts. ANSI rendering must be safe; never execute escape sequences that can inject terminal control in web UI.

## Cross-machine isolation

All events carry machine/repository/worktree/revision/run identifiers. Running identical test names in two worktrees must never merge their state.

## Exit gate

Representative fixture projects for Python, Go, Rust, Java and Scala display correct trees, deliberate failures show their traces/source links, rerun operations invoke native commands, and parallel worktrees do not cross-contaminate events.

---

# 0130 — Woodpecker / Pileated CI Integration

## Goal

Make remote CI feel like another pane of the distributed IDE while preserving the CI system as execution authority.

## Provider layer

Implement a Woodpecker-compatible provider contract using `CIEvent`. Pileated extensions are capability-gated optional fields/actions so normal Woodpecker pipelines remain valid.

## Required views

- repositories and recent pipelines;
- pipeline state/duration/ref/commit;
- step tree and step logs;
- queue order/wait time where exposed;
- runner inventory/state/labels/capacity where exposed;
- commit/PR links;
- correlation back to HerdR session/mission/worktree/test evidence when reliable.

## Actions

When supported and authorized:

- retry pipeline/step;
- cancel run;
- open provider UI;
- create a “Debug with Pi” handoff containing revision, failing step, bounded log summary and artifact references.

“Debug with Pi” starts/targets the owning Pi workflow through its API; it does not make HerdR the mission orchestrator.

## Freshness

Display provider timestamp/refresh state. Network/API failure becomes `stale`/`unavailable`, never guessed success.

## Exit gate

Against a real Woodpecker/Pileated-compatible environment or controlled integration fixture, HerdR shows pipelines, logs, queue and runners; correlates a known commit; securely retries/cancels only when authorized; and can hand a failing run to Pi with bounded structured context.

---

# 0140 — Multi-Agent Worktree and Candidate Comparison

## Goal

Turn parallel Pi Engineering candidates into an understandable comparison workspace rather than dozens of anonymous sessions and ports.

## Candidate model

A candidate is a projection over existing ownership identifiers, not a new executor:

```text
mission/issue
  -> worker/session
  -> machine
  -> repository/worktree/revision
  -> diff
  -> dev-service lease(s)
  -> tests
  -> CI
  -> browser artifacts
  -> review
```

## UX

For one mission/issue, show candidate cards/table with:

- current status/attention;
- machine/session/worktree/branch;
- concise changed-file/diff summary;
- test and CI status;
- review state/findings summary;
- live `dev:<port>` previews;
- screenshots/browser error count;
- artifacts and latest activity.

Support side-by-side or rapid-switch preview, diff comparison and evidence inspection. Do not impose an opaque AI “winner” score unless an owning workflow explicitly provides evaluation criteria/results; preserve raw evidence and reviewer outputs.

## Lifecycle

Candidate cleanup must coordinate with the owner: close dev leases, materialize retained evidence, expire activity links as needed, then request/observe worktree removal. Never delete another worker's worktree based on path/name heuristics.

## Exit gate

A test mission with at least six concurrent isolated worktrees across multiple hosts can be grouped correctly, each preview/evidence set remains distinct, comparisons remain usable on desktop and reduced mobile view, and cleanup cannot remove another candidate's resources.

---

# 0150 — Unified Engineering UX

## Goal

Combine the capabilities built so far into one coherent, layered experience rather than a collection of plugin tabs.

## Information architecture

Primary navigation should converge on:

- **Fleet** — machines, connection/health/capabilities;
- **Sessions** — agents and CURRENT/TIMELINE/SUMMARY/RELATED;
- **Missions** — semantic Pi/AutoSpec grouping and candidates;
- **Previews** — dev services/browser evidence;
- **Tests** — local/worktree test tree/history;
- **CI** — pipelines/queues/runners;
- **Artifacts** — shared evidence/handoffs;
- **Attention** — blocked/approval/failure items.

Avoid duplicating the same session list in unrelated plugin pages. Existing plugin UIs may be embedded/deep-linked but should inherit shared navigation/context when possible.

## Shared context bar

Where space allows, show machine → repository → worktree/branch → session/mission, with direct navigation. On phone, collapse progressively while retaining identity and attention state.

## Search/goto

Provide cross-surface search/goto by machine, repo, issue/mission, session, worktree, branch, test, CI run and artifact. Search results navigate to canonical owning views.

## Performance

- Lazy-load heavy logs/browser views.
- Virtualize long timelines/test lists.
- Avoid polling every machine independently from every browser tab; centralize/subscription-cache where supported.
- Mobile remains responsive with large fleets and many sessions.

## Accessibility

Keyboard navigation on desktop, touch targets on mobile, semantic status labels beyond color, and readable log/test failure rendering are required.

## Exit gate

A usability walkthrough can complete the five master user journeys without navigating unrelated standalone tools manually, while deep links remain stable and phone/tablet layouts preserve core steering/review functions.

---

# 0160 — Attention, Notifications and Review Actions

## Goal

Surface the small subset of a large agent fleet that actually needs human attention without creating notification spam.

## Attention model

Normalize actionable conditions such as:

- agent blocked or awaiting input;
- explicit approval/review requested;
- failed tests after worker completion;
- failed CI for an active mission;
- dev preview/browser validation failure;
- unhealthy host/service that affects active work;
- completed candidate set ready for comparison.

Attention items link to the exact session/mission/test/CI/review evidence and have lifecycle (`open`, `acknowledged`, `resolved`, `superseded`).

## Notifications

Prefer audited native/Herdr notification plugin behavior and add policy/aggregation around it. Support local/browser notifications first; optional external channels may be adapters.

Policy must provide:

- severity thresholds;
- dedupe/coalescing;
- quiet hours/configurable routing;
- per-project/user overrides;
- no secrets/log dumps in notification payloads;
- link back to the private HerdR view.

## Review/approval actions

Actions execute in their owner systems (Pi Engineering, Plannotator, CI, etc.) and require capability + authorization. The attention center records the observable action/result, not a second approval state machine.

## Exit gate

A simulated fleet producing repeated identical blocked/failure events creates one coherent attention item, sends policy-compliant notification(s), links to evidence, supports an authorized action and resolves when the owning system reports completion.

---

# 0170 — Observability, Security, Updates and Rollback

## Goal

Make the complete system diagnosable and safely operable before calling it production-ready.

## Observability

Use correlated IDs across machine/session/mission/worktree/dev lease/test/CI/artifact. Expose metrics/logs for:

- machine/API connectivity and reconnects;
- plugin capability health;
- event ingestion lag/errors/dedupes;
- artifact publication failures;
- dev lease allocation, active count, route health and expirations;
- test adapter runs/events/errors;
- CI API freshness/errors;
- web/mobile latency and websocket reconnects;
- notification delivery/dedupe;
- version/lock drift.

Prefer OpenTelemetry-compatible instrumentation where practical. Never put secrets or hidden reasoning in telemetry.

## Doctor

`doctor` must have concise human output and stable JSON. Checks include versions/capabilities, private/public listeners, Tailscale reachability, config/lock validity, shared workspace, dev fabric, adopted plugins and relevant external providers.

## Security baseline

- least-privilege tokens/scopes;
- secrets from environment/secret manager, never repo;
- no public listener by default;
- validate URL/path/input boundaries;
- protect terminal/control actions with authz and audit;
- sandbox/escape user-supplied text in web log/diff/test rendering;
- do not allow arbitrary route targets through dev fabric;
- dependency pinning and update audit;
- document threat model and recovery procedures.

## Update lifecycle

```text
refresh audit -> compatibility test -> stage/canary host -> fleet rollout -> post-check
```

Keep previous known-good lock/config. Failed compatibility or doctor check blocks rollout. Rollback restores lock/config and restarts affected managed services safely.

## Exit gate

Fault injection for remote host loss, plugin crash, event duplication, shared storage loss, dev router restart and CI API outage produces bounded degraded behavior; security checks detect an intentionally public listener; update canary rejects a broken dependency; rollback returns to green acceptance checks.

---

# 0180 — Fleet Validation, Release and Documentation

## Goal

Prove the whole product from fresh install through rollback and ship documentation that matches reality.

## Full acceptance run

Execute every row in `docs/acceptance-matrix.md` across representative Linux/macOS hosts and a phone/iPad tailnet client. Include a high-concurrency scenario with dozens of sessions and at least six parallel candidate worktrees.

## Fresh install

From documented prerequisites only:

1. enroll a clean supported host through Ansible;
2. install/pin the Powerpack;
3. connect it as a Herdr machine;
4. run Pi through `https://llm.metabolomics.us`;
5. open from web/mobile;
6. create session/worktree/artifact/dev service/test evidence;
7. observe CI when configured.

No undocumented manual fixes are allowed.

## Upgrade + rollback

Validate upgrade from the previous pinned release/profile and deliberate rollback. Record exact state that is preserved (sessions, user config, activity database, artifacts, leases/reconciliation behavior).

## Documentation

Finalize:

- README product story and screenshots/architecture diagram where useful;
- installation and fleet enrollment;
- plugin/dependency policy;
- web/mobile use;
- dev fabric use and troubleshooting;
- session timeline semantics/privacy;
- test/CI integration;
- operator/security/update/rollback guide;
- contributor/plugin-adapter guide;
- known limitations and supported versions.

## Release artifacts

- immutable dependency lock;
- tagged release/change log;
- validated Ansible/config examples;
- machine-readable compatibility matrix;
- completed acceptance evidence/report.

## Exit gate

Every non-external-blocked acceptance item is green, fresh install and upgrade/rollback drills pass, documentation commands are copy-tested, and the release can be recreated from Git + declared external dependencies without hidden workstation state.

---

