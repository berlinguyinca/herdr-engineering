# Release safety audit

## Verified corrections

- Native launcher exceptions retain committed capacity; replay requires reconciliation.
- Commit retry preserves lease expiry and cannot revive a fenced execution lease.
- Heartbeat renewal rejects expired leases before the reconciliation sweep runs.
- Checkpoint IDs reject traversal and checkpoint agent identity must match its lease.
- Generic host ingestion cannot set stable UID, scheduler ownership, trust, or policy.

Fresh validation: 52 tests passed across ingestion, scheduler persistence, service,
adapter registry, scheduler domain, and checkpoint/resume. Targeted Ruff and
`git diff --check` passed. Tests demonstrated each defect before its correction.

## Release blockers still requiring implementation

Cross-process transactional admission, acknowledged durable scheduler writes,
native launcher ownership/fencing, actual runtime integration, and representative
fault injection remain incomplete. A lease marked fenced in memory does not
prove that the original process has lost workspace write access. The existing
fault inventory is descriptive, not an executable fault-injection suite.

Automatic migration must remain disabled until these blockers are resolved.

## Admission persistence continuation

Reservation and lease projections now call the acknowledged event persistence
path before updating repository state. A failing durable sink propagates the
failure, with tests proving that neither projection acknowledges an unwritten
record. In-memory development mode remains possible without a configured sink;
this does not establish production durability or cross-process atomic admission.

Admission now rejects negative, non-finite, boolean, and nonnumeric CPU, memory,
and disk requests before capacity calculations. Fresh combined ingestion,
persistence, service, adapter, scheduler, and checkpoint suite: 59 passed.

## Transactional admission continuation

The PostgreSQL sink now provides an admission transaction using an advisory
transaction lock before reading latest reservation events. The service reloads
authoritative reservation demand under that lock and persists new admission
before commit. Shared repository service instances use the repository command
lock. A 20-request, two-service concurrency test admits exactly the two requests
that fit the host. Transaction boundary tests prove lock/read ordering and rollback.

Fresh focused suite: 37 passed; targeted lint and diff checks passed. This is
unit/concurrency evidence; live multi-process PostgreSQL contention and crash
injection still require execution before claiming distributed admission verified.

## Live PostgreSQL verification

`tests/control_plane/test_scheduler_postgres.py` provisions and stops disposable
PostgreSQL instances. The real contention test submits 100 requests across four
independent processes and observes exactly 10 reservations for 10 CPU cores,
with the other 90 queued. The crash test terminates a coordinator with
`os._exit` after its event insert and before transaction commit: a subsequent
transaction observes no reservation and can acquire the admission lock.

Fresh run: 2 passed in 87.08 seconds; targeted Ruff and whitespace checks passed.
This proves these admission scenarios, not workspace fencing, policy races
across coordinators, launch ownership, or the entire recovery fault matrix.

## Atomic execution claims

The registry now acquires a persisted execution-submission claim before invoking
its native launcher. Commit and claim use the PostgreSQL admission transaction;
commit retries preserve the authoritative reservation and any existing claim.
Scheduler stream sequence numbers are read from PostgreSQL before append.

Fresh unit suite: 34 passed. A real PostgreSQL test submits eight claims for one
request across four independent processes: exactly one claim succeeds and seven
are denied (1 passed in 11.56 seconds). No agent processes are launched by this
test. Targeted Ruff and diff checks pass. This verifies submission ownership,
not revocation of workspace access from an already-running process.

## Collaboration launcher ownership integration

Scheduler-enabled collaboration launches now acquire the persisted execution
claim through the existing authenticated API before creating native panes.
Repeated claims return HTTP 409. A native timeout retains committed capacity
and the agent credential/identity for reconciliation instead of releasing
capacity and revoking an agent that may be running.

Fresh API, complete collaboration spawn, adapter, and persistence selection:
57 passed in 44.20 seconds. Targeted Ruff and diff checks passed. The current
fleet result records an explicit reconciliation-required diagnostic for uncertain
execution; a dedicated operator reconciliation UI is still required.

## Operator placement visibility

Scheduling overview and queue APIs now read reservation state instead of fixed
zero counts. The dashboard shows claimed executions (explicitly distinct from
confirmed running processes), retained CPU demand, and bounded placement history.
History uses an output field allowlist excluding lease credentials. Stale hosts
no longer contribute to eligible-host counts.

Fresh Python service/persistence/API selection: 23 passed. Two scheduler view
tests pass, including escaped history identities. Targeted Ruff and diff checks
passed. Verified native process reconciliation controls remain outstanding.

## Policy editing and browser verification

Policy mutations now serialize revision checks and await durable persistence
before updating host state. A storage-failure regression proves the previous
policy remains intact. Host policy events replay through the existing bridge
without refreshing host availability. Naive telemetry timestamps fail closed.

The Scheduling view includes a confirmation checkbox and policy selector with
revision conflict feedback. A real HTTP/Playwright interaction test verifies
policy persistence after reload and confirms placement previews never reserve.
The browser test exposed and fixed global SSE refresh clearing policy edits.
Screenshots were captured at 390, 768, 1440, and 3840 pixels under
`tests/playwright/screenshots/scheduler-*.png`; the mobile capture was inspected.

## Queued request recovery

Reservations persist normalized original request context. Explicit queued retry
reloads authoritative demand under the admission transaction, re-evaluates policy
and capacity, and preserves the reservation ID. A reserved/committed request
cannot be retried through this operation. Dashboard queued history exposes the
retry action through the existing operator API. Fresh service/persistence/domain
selection: 27 passed; two view tests passed, and targeted lint/diff checks passed.
Automatic fair-queue draining and workload lifecycle integration remain required.

## Automatic fair queue admission

Queue age is persisted. A bounded drain pass ranks work by aging-adjusted
priority and active project demand, and reserves under the PostgreSQL admission
lock. A restart test gives two newly available slots to two different projects
and verifies no further admission beyond capacity. The control-plane sweep
supports automatic reservation draining through the explicit
`control_plane.scheduler_admission` flag, default false. This admits queued
reservations; family launchers still own execution submission and claims.
Expiry reconciliation now reloads authoritative reservations under the same
lock, avoiding release based on a stale coordinator projection.

## Reservation ownership and idempotency

Existing request IDs now require matching owner and normalized original placement
constraints before returning a reservation. A different owner cannot retrieve
the reservation, and changed resource constraints produce a conflict. Family,
placement mode, workload class, priority, and explicit manual host selection are
validated against the placement contract before admission. Fresh service,
persistence, adapter, and domain selection: 38 passed; targeted lint/diff passed.

## Cross-coordinator policy races

Policy mutations now use the PostgreSQL admission lock. Each admission/bind/claim
refreshes authoritative policy events under that lock. A real PostgreSQL test
reserves through one coordinator, disables the host through another, and proves
the first coordinator cannot commit (1 passed in 27.66 seconds). Execution claims
also recheck policy generation and eligibility after commit. Fresh service and
persistence checks: 31 passed; the preceding host/service/persistence selection
passed 33 tests. Targeted lint and diff checks passed.

## Bind authorization and capacity

Bind preserves the persisted explicit manual mode and host selection, rechecks
resource tags, rejects expired reservations before any sweep, and rejects lost
capacity using current measured demand plus active reservations. Draining hosts
deny explicit manual pins as well as automatic placement. Regressions cover
manual-only commit/claim, draining-host denial, capacity drop, and expiry.

## Host resource reporting integration

The existing fabric agent now samples resource telemetry every 15 seconds on its
normal scan loop, using fleet host credentials and the configured control-plane
URL. Stable local identity is persisted in a private file. Linux CPU utilization
uses deltas from /proc/stat; PSI, available memory, and disk capacity are sampled
without process arguments. Missing metrics remain unknown and block capacity.

The fleet resource endpoint derives host identity from its token, validates
timestamps and sequence, rejects UID conflicts, preserves existing policy, and
defaults macOS to manual-only. A real HTTP test rejects an operator API key in
place of host credentials and accepts the configured host token. Headroom and
pressure constraints feed the scheduler capacity projection.

Fresh resource/fabric-agent/service suite: 65 passed in 22.67 seconds; HTTP test:
1 passed. Targeted lint, compilation, and diff checks passed. macOS resource
accounting, per-agent cgroups, detailed GPU diagnostics, and sustained-pressure
hysteresis still need completion before full P02 acceptance.

## macOS resource observations and pressure explanations

The resource probe now supports bounded macOS sysctl/vm_stat/ps observations.
CPU is explicitly labeled a process estimate; PSI and cgroup accounting are
marked unsupported. Parser tests verify memory page conversion, numeric-only
CPU probing, timeouts, and partial confidence. Read-only probes of the configured
Mac confirmed the expected formats and 16-KiB page size.

Pressure and metric gaps propagate structured exclusion codes into placement
decisions, and bind rejects hosts with resource-health exclusions. Fresh resource,
domain, service, and telemetry selection: 33 passed in 17.49 seconds; targeted
lint/diff checks passed. This does not certify macOS live migration or full
per-process accounting.

## Native family callback implementation

Installed Herdr help confirmed workspace creation and `agent start --kind --pane`
for pi/claude/codex. The adapter now implements those commands with validated
names, prepared absolute paths, machine/session routing, and acknowledged native
results. Its worktree helper was corrected from nonexistent `worktree add` to
the installed `worktree create --path --branch --cwd --base` contract.

The registry now offers concrete Pi, Claude Code, and Codex callbacks requiring
host-bound prepared-workspace ownership before invoking native agent start.
Fresh adapter/registry/service selection: 42 passed; targeted lint, compilation,
and diff checks passed. Production manifest resolvers/host-runner wiring and
actual native process lifecycle verification remain required; this is contract
test evidence and does not claim that real agents were launched.

## Host-side Git preparation and registry artifacts

Native preparation now verifies the source repository origin, clean Git state,
exact full commit, branch syntax, destination absence, and disk headroom. It calls
the existing Herdr worktree operation and independently verifies destination HEAD
and branch before publishing readiness. Actual Git tests preserve dirty source
files and reject a second worktree using the same branch.

Artifact references use the existing immutable-version materializer with ACL
checks and checksum acknowledgment, under a separate workspace artifact directory.
Admission rejects a second active request for the same workspace (`WORKSPACE_LOCK`)
until the existing reservation is released. This prevents competing scheduler
launches but does not revoke writes from arbitrary external processes.

## Supervised Linux write revocation

`SystemdExecutionScope` reuses the collaboration sandbox's private PID and mount
namespaces inside a named systemd service cgroup. An external-scope flag prevents
the sandbox from creating a separate scope outside that cgroup. Stop requires
matching receipt identity, successful systemd stop, and an empty or removed
cgroup before acknowledging termination. A host-side service operation binds
that proof to the reservation/lease before releasing capacity. Ordinary release
rejects claimed executions that lack verified stop.

Live test `HERDR_SCHEDULER_SCOPE_LIVE=1 pytest tests/test_execution_scope_live.py`
starts a real sandbox writer and a child with a new session, stops the service,
and confirms both files stop changing: live/unit selection 2 passed in 0.69s.
Service/persistence/registry/scope selection: 44 passed. Targeted lint, shell
syntax, and diff checks passed. Native interactive panes are not automatically
certified by this test; they must explicitly use the supervised boundary.

## Process-tree resource accountability

Supervised execution receipts now support cgroup-v2 CPU seconds, current/peak
memory, I/O bytes, and CPU/memory/I/O pressure. Missing or invalid metrics are
explicitly unknown with source errors. The host-side service persists observations
against reservation identity, rejecting mismatched agents/hosts/generations.
The real sandbox workload confirms positive CPU and memory counters before
verified stop. Live and unit scope selection: 3 passed in 0.37 seconds; targeted
lint/diff checks passed. Periodic reporting for all native families remains to wire.

## Historical simulator integration

The simulator API and dashboard panel rescore persisted candidate snapshots using
operator-selected CPU/memory weights and 24-hour/7-day/30-day history windows.
Recorded hard exclusions remain hard exclusions; missing historical capacity
produces an unknown result. Preview never changes policy or reservations and
labels independent snapshots/counterfactual limitations without performance claims.

Fresh domain/service selection: 32 passed. Two real HTTP/Playwright tests verify
policy editing, read-only placement previews, and simulation preserving reservation
history (2 passed in 13.53 seconds); two view tests passed. Targeted lint/diff passed.
Forecasting workload dependency curves and measured baseline pilot comparison
remain required for full simulator/capacity-planning acceptance.

## Broad regression and live fleet policy evidence

The accumulated scheduler/resource/native-adapter/workspace/checkpoint/ingest/
bridge/sink/fabric-agent selection passed 187 tests in 79.48 seconds. The configured
Mac reports the existing control plane at `http://100.104.39.6`; the live scheduler
shows its manual-only policy. Legacy fleet opt-outs were explicitly written and
read back for both `mac.lan` and `macbook-pro` (`auto_agents=false`). Initial HTTP
timeouts were reconciled by querying the resulting state, not assumed failures.

Read-only host checks verified Herdr 0.9.1 at beast's `~/.local/bin/herdr`;
the earlier PATH-only missing-install conclusion was incorrect.

GitHub acceptance tracking: https://github.com/berlinguyinca/herdr-engineering/issues/290
The issue explicitly separates implemented components from unfinished acceptance.

## Legacy rollback exclusion enforcement

The broad legacy compatibility run finished with 48 passes and one HTTP claim
timeout; the timed-out case passed alone (1 passed in 8.58 seconds). This remains
a recorded broad-suite timing failure, not a fully green suite claim.

A separate exclusion defect was corrected: parent-host affinity in legacy mode
now passes through current eligible host flags instead of bypassing opt-out.
Missing flags fail closed. Targeted integration verifies helpers avoid an
opted-out parent host while retaining lineage, eligible parents preserve the
existing split-tab workflow, and all-excluded fleets create nothing: 3 passed
in 91.83 seconds. Packaged/deployed sandbox copies match; shell syntax, targeted
lint, and diff checks passed.

## Resume and authoritative recursive budgets — 2026-10-28

User explicitly resumed Codex after the paused handoff. The Pi package remained
queued/unclaimed, with no active scheduler test process from this session.
Current edits derive child depth from authoritative persisted parent chains,
reject unknown/foreign/cyclic ancestry, inherit mission/project scope, and apply
parent concurrency and burst limits across runtime families/restarts. Retry,
automatic drain, and preview use the same ancestry checks. External legacy parent
registration remains an integration task; unknown context fails closed.

Fresh scheduler/service/persistence/registry selection: 55 passed in 0.26 seconds;
targeted lint/diff checks passed. Tests exercise caller depth reset, scope escape,
foreign/unknown parents, and restarted alternate-family fanout.

## Resumed execution gates and reviewed recovery hardening — 2026-10-28

Execution commit now refuses repositories without an acknowledged durable writer.
The HTTP admission flag gates reservation/commit/claim/retry/native launch routes;
previews remain available while admission is disabled. Five scheduler authority
stream types are rejected by generic ingestion, and per-entity HTTP events redact
lease credentials consistently with global activity/SSE.

Recovery requires explicit migration enablement, a version-bound runtime adapter,
and a durable journal. Runtime checkpoints are verified before stop; destination
restore requires verified origin stop/fencing and a higher epoch, followed by
heartbeat/workspace/single-writer verification. Interrupted or uncertain operations
remain reconciling and cannot replay from the beginning. Fault inventories now
explicitly mark unexecuted cases pending instead of reporting fabricated outcomes.

A PostgreSQL crash test proves recovery transitions commit before coordinator
death. A dedicated session lock serializes recovery while individual transition
events commit independently. Fault-injected protocol tests cover busy tools,
checkpoint capture/storage/corruption, uncertain stop/fence, stale generation,
restore timeout, missing heartbeat, missing durability, and restart idempotency.

Independent review found five defects in checkpoint atomicity/path identity and
scope receipt binding. Fixes use immutable single-file atomic publication, pinned
private directory FDs, no-follow regular-file access, expected lease validation,
systemd invocation/cgroup/kill-mode binding, and uncertain-start cleanup metadata.
The reviewer rechecked and approved this bounded scope after fifteen additional
in-memory checks; no remaining blocker was found in those two modules. This is
not a full-product approval or native-runtime migration certification.

Fresh evidence: all four existing PostgreSQL admission tests passed (11.33s);
new recovery-journal crash test passed (3.96s); admission/API/browser selection
passed 47 Python and two Playwright tests; combined recovery/scheduler/scope/
workspace selection passed 93 tests; post-fix scope live/unit selection passed
four tests, including real parent/child write revocation. Generic authority
ingestion and recovery/checkpoint selection passed 85 tests. Targeted Ruff and
diff checks passed. Full Python regression run remains in progress at
`/tmp/herdr-scheduler-full-psIugBbB.log`; do not infer its final result.

## Active execution budgets

Shared placement now enforces owner and project active concurrency budgets,
including queued retry and automatic drain; over-budget requests are queued with
USER_QUOTA/PROJECT_QUOTA. Workload profiles receive conservative CPU/RAM/disk
defaults when omitted. Deployment-owned scheduler limits are configurable and
preserved whenever durable state is reloaded. Fresh service/persistence/domain
selection: 46 passed; API smoke, targeted lint, compilation, and diff checks passed.
Full per-mission budgets, learned profiles, and runtime hibernation certification
remain part of the outstanding acceptance checklist.

## Prepared workspace resolver and service launch integration

Workspace manifests and destination readiness records now persist through the
existing acknowledged event store and resolve after restart by workspace and
host UID. Revision/path validation occurs before marking readiness. The scheduler
native launch service uses this resolver and saved Herdr machine aliases, obtains
the execution claim, and invokes the concrete native callback. The operator launch
API is wired; deployments must explicitly enable `native_scheduler_launcher`.

A service integration test resolves a persisted destination workspace, invokes
one native callback, and blocks a repeated launch. Workspace/registry/service
selection: 34 passed; targeted lint, compilation, and diff checks passed.
Real destination preparation evidence, artifact materialization, process-backed
readiness, and runtime lifecycle reconciliation remain required for canary release.
