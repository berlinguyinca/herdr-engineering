# Fleet Pi runtime rollout

`herdr-eng fleet rollout` pins one complete Pi commit and reconciles existing
HerdR Pi sessions through staging, preparation, activation, and mission recovery.
It uses structured Pi control requests. It never sends prompts, creates replacement
missions, kills sessions, or clears an operator pause.

## Prepare and review

Use a tested, published **full 40-character lowercase commit SHA**. Branch names
and tags are rejected. The deployment helper verifies the selected commit and
installs its dependencies under
`~/.local/share/herdr-engineering/pi/releases/<sha>` on each selected machine.

```sh
herdr-eng fleet rollout plan --pi-revision FULL_COMMIT_SHA --host bender
# Omit --host to select every host in the private fleet inventory.
# Optional: --inventory /absolute/path/hosts.yml --state /private/state/directory
```

Planning reads the Ansible inventory and resolves host connection variables. It
writes an immutable host/release plan and returns its `id` as JSON; it does not
install code or contact agents. Host names must match the saved native HerdR
machine names. SSH transport uses the frozen `ansible_host`, `ansible_user`, and
`ansible_port`; configured SSH aliases and keys must already work in batch mode.
Custom Ansible-only connection plugins, become, and password prompts are not
supported by the Pi control transport.

## Apply and observe

```sh
herdr-eng fleet rollout apply --id ROLLOUT_ID
herdr-eng fleet rollout status --id ROLLOUT_ID
```

`apply` executes one bounded, serial reconciliation pass. Repeat the same command
to poll in-flight receipts, retry offline hosts, and observe progress. A service or
scheduler may run it periodically; overlapping applies are refused by a private
file lock. Exit code 0 means all recorded hosts and sessions verified; code 2
means pending/blocked work remains, and code 1 indicates an invalid plan or local
execution error. `status` only reads the saved JSON and reports the last observed
state; it never probes, stages, resumes, or rewrites state.

Per-host work is serial so one rollout does not create a fleet build storm.
Transport calls have timeouts; a stopped controller resumes from durable state.
Requests are saved before sending and receipts are saved atomically with fsync.
Uncertain deliveries reuse the original operation ID and payload. A replacement
runtime instance in the same process polls the same durable receipt.

The default state directory is `~/.local/state/herdr-engineering/rollouts`, mode
0700, with 0600 JSON files. Use the same directory after restarting the controller.
Do not edit it to change a revision, destination, process, or operation ID. Create
a separate rollout for another release. There is no automatic downgrade or
cross-rollout rollback: a rollback is a separately reviewed pinned release plan.

## What completion proves

Every planned host must have staged the exact selected release and expose complete
native session coverage. Each Pi agent must have an exact native conversation
reference that matches one reachable control descriptor. Missing references,
duplicate conversations, missing sessions, and PID changes block activation;
terminal titles and working directories are never used as identity guesses.

The coordinator verifies both the loaded runtime commit and the stable Host commit
against the pinned SHA. A successful reload receipt alone is insufficient. Mission
resume requests carry the original mission ID, revision and resumption generation.
Explicitly paused and terminal missions are preserved. Eligible existing missions
must exhibit a later meaningful-progress timestamp or a named future wait after
activation; resume acknowledgment or a running status alone does not complete the
rollout. An expired wait is re-evaluated on the next apply.

The plan retains native terminal/session identity, Pi PID and instance, requests,
attempt counts, receipts, loaded provenance and progress evidence. A disappeared
previously discovered session prevents complete coverage. A host with no discoverable
Pi agents remains coverage-unverified rather than silently succeeding.

## Pending and bootstrap cases

- `pending`: a host is offline, staging is unavailable, or an operation is running.
  Restore connectivity and repeat `apply` with the same ID and SHA.
- `awaiting_progress`: the release is loaded but useful mission work has not yet
  been observed. Continue monitoring; the coordinator does not manufacture progress.
- `activation_unverified`: Pi acknowledged reload but still reports old or pending
  code. Inspect the saved activation receipt and Pi host diagnostics.
- `bootstrap_required`: a session lacks maintenance capabilities, or its stable Host
  commit differs from the target. Runtime hot reload cannot replace that stable
  Host. Preserve its conversation and work, then perform a controlled extension or
  process bootstrap before a new reconciliation plan. This command intentionally
  provides no forced kill/restart fallback.
- `blocked`: identity, protocol, revision, or coverage evidence was rejected.
  Inspect the per-host error and per-session receipt. An uncertain request is never
  replaced with a fresh resume against a different mission revision.

Existing sessions without the new control API need this explicit first bootstrap.
An unreachable machine remains pending on the original immutable release. Operator
pauses survive maintenance and do not count as a mission recovery failure.

## Validation coverage

`tests/test_fleet_rollout.py` exercises real durable state and the production JSON
transport through a subprocess fixture: two hosts, offline recovery, controller
restart and lost replies, exact replay, old loaded code, stable Host mismatch,
operator pause, busy preparation, duplicate/stale identities, missing native
coverage, corrupt receipts, progress regression and expired waits. These tests do
not constitute a live multi-workstation rollout; validate a canary before fleet
activation.
