# Fabric scheduler reconciliation — 2026-11-04

This supersedes the implementation-status claims in the historical phase log
and launch-path matrix. The ZIP remains the original requirements baseline.
Tracking: [#290](https://github.com/berlinguyinca/herdr-engineering/issues/290).

## Audit boundary

Inspected fetched `origin/main` at
`42259d55fc58bbf3fbca04a01df9443dcfec97d6` (2026-11-04), merged PR history,
and current source, tests, and operations/design records. The remote default
branch is `main`; no `origin/master` was advertised. This was a repository audit,
not a fresh test run or verification of deployed flags, hosts, or services.
Previously reported test results and live Mac opt-outs remain historical evidence.

## Reconciled requirements

| Original fabric phase | Current evidence | Acceptance status |
| --- | --- | --- |
| P00 audit | Original archive/audit preserved; this review refreshes changed architecture and decisions | Audit refreshed; the historical launch matrix is not enforcement proof |
| P01 host policies | `host_policy.py`, authenticated observations, manual-only defaults, and parent opt-out fix #327 | Foundation merged; all-path and alias/reconnect acceptance remains unverified |
| P02 telemetry | `resource_probe.py`, existing fleet host loop, token-bound observation API and durable refresh in #322 | Merged; current live coverage and measured fleet behavior not revalidated |
| P03 admission | `scheduler.py`, `scheduler_service.py`, repo/sink admission transactions, queue caps, execution claims, rollback and snapshot tests in #322 | Merged component; does not establish physical-launch coverage |
| P04 launch adapters | `AdapterRegistry` is referenced by its module/tests only; the runner reserves/commits but does not claim execution or retain heartbeat credentials | Not accepted; native-launch expansion deferred by the newer unified-workspace decision |
| P05 distributed workspaces | `workspace.py` has in-memory manifests and a supplied preparation callback; native adapter still constructs `worktree add` | Prototype only; destination execution, Git verification, and durable preparation are not established by current main |
| P06 quotas/lifecycle | Persisted resource/concurrency/ancestry/workspace constraints and fair queue retry/drain in #322 | Quotas merged; complete native process lifecycle/accounting remains open |
| P07 dashboard | Existing visibility-aware polling/morphing, host cards, placement preview, and truthful claim counters | Partial: policy mutation exists in the API and history in overview data; editing/history/simulator UI is absent |
| P08 recovery | Atomic private checkpoints #305 and invocation-bound Linux execution scopes #307 | Primitives merged; no all-family checkpoint/restore or migration certification |
| P09 replay/advisory | `replay.py` and tests provide library functionality | No scheduler simulation route/view; complete persisted historical replay and advisory acceptance not established |
| P10 rollout | Runbooks, feature gates, and historical component/test evidence | Current deployment, fault/load comparison, multi-host canary, and rollback acceptance remain unverified |

Merged evidence includes #305, #307, #308, #322, and #327. A merged helper or
passing unit test does not close the corresponding end-to-end phase gate.

## Newer decisions and reuse

The unified-workspace records explicitly say scheduler placement is skipped by
choice and the native-launch stack stays archived on `salvage/*`:

- [STATUS.md, preserved decision](https://github.com/berlinguyinca/herdr-engineering/blob/42259d55fc58bbf3fbca04a01df9443dcfec97d6/docs/specs/herdr-unified-workspace-20261028/evidence/STATUS.md#L32)
- [P05 result and decisions](https://github.com/berlinguyinca/herdr-engineering/blob/42259d55fc58bbf3fbca04a01df9443dcfec97d6/docs/specs/herdr-unified-workspace-20261028/evidence/P05-RESULT.md)

This is a recorded deferral in that integration workstream, not evidence that
the original fabric launch requirements were completed or a new cancellation
of the whole scheduler project. Do not resume the archived stack automatically.
Any renewed native-placement implementation must first reconcile its scope
with that recorded decision and current runtime contracts.

Reuse these newer implementations when evaluating remaining work:

- `assistant_pi_runner.py` and `CollabService` assistant-runtime routes already
  implement Pi runtime claims, launch attestations, heartbeats, cancellation,
  and reports. These are package-runtime lifecycle contracts, not proof that
  every Pi/Claude/Codex launch passes through resource admission.
- `docs/operations/fleet-runtime-rollout.md` describes pinned Pi activation and
  recovery of existing sessions with identity/fence evidence. That rollout is
  not a resource-placement canary or cross-host migration certificate.
- Team task admission/leases (#482), `PgLeaseStore` (#485), review independence
  (#489), completion replay (#495), opt-in takeover (#500/#501), and fence repair
  (#538) cover logical task authority. `PgLeaseStore` has no production caller
  in the inspected tree. Avoid adding a parallel task-lease store or equating
  task fencing with OS process-tree fencing.

Unified-workspace phase numbers are a different plan: its P05 teams/tasks are
not the original fabric P05 distributed-worktree phase.

## Local work and confirmed gaps

The local `heartbeat_for_host` implementation and
`/api/v1/fleet/runtime/leases/heartbeat` endpoint are absent from inspected main.
They remain unmerged experimental work. Their authorization and failure
semantics must be compared with the existing Pi runtime callbacks before any
decision to integrate them; they are not the automatic next implementation.

The generic registry currently releases a committed reservation on launcher
exceptions and does not call `claim_execution`. Its test explicitly expects
that release. This is not evidence of safe uncertain-launch handling. Preserve
the distinction before activating any archived adapter.

Keep #290 open with these remaining gates visible:

1. Reconcile renewed native-placement scope with the recorded deferral and reuse
   current runtime/task contracts; do not create a second lifecycle framework.
2. Record evidence per actual launch path before claiming global enforcement.
3. If retaining scheduler UI work, complete the missing controls/history against
   existing APIs and the current mounted view, preserving edited forms.
4. Treat distributed preparation and family-specific migration as unaccepted
   until real destination/process evidence exists; keep unsupported migration off.
5. Revalidate deployment state, fault/load behavior, canaries, and rollback before
   asserting release readiness. Do not infer live state from a merged PR.

InferWeave retains model inference and GPU-worker scheduling ownership. No
runtime code, fleet settings, production services, or deployment flags were
changed by this reconciliation.
