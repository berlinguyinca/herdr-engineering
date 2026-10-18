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

Planning resolves the full Ansible `herdr_fleet` group closure, including direct
hosts and recursively nested groups, and freezes host connection variables. It
writes an immutable host/release plan and returns its `id` as JSON; it does not
install code or contact agents. Native HerdR inventory is read on each target
workstation through the same frozen local/SSH transport used for Pi control; no
controller-side saved machine registration is required. SSH transport uses the
frozen `ansible_host`, `ansible_user`, and
`ansible_port`; configured SSH aliases and keys must already work in batch mode.
Set `herdr_session` in a host's inventory variables to select and freeze a named
HerdR server session; otherwise that target host's default server scope is used. Before
staging, the resolved target is checked against the plan; changed addresses, users,
ports or named sessions block deployment and require a new reviewed plan.
Remote native and Pi commands prepend `~/.local/bin`, `/opt/homebrew/bin`, and
`/usr/local/bin` to the noninteractive SSH PATH, using a fixed shell prefix and
individually quoted arguments. Failed transports report only known stderr error
categories; arbitrary logs and possible credentials are withheld.
Custom Ansible-only connection plugins, become, and password prompts are not
supported by the Pi control transport.

## Apply and observe

```sh
herdr-eng fleet rollout apply --id ROLLOUT_ID
herdr-eng fleet rollout status --id ROLLOUT_ID
```

`apply` executes one bounded, serial reconciliation pass. For unattended updates,
run the durable controller against the **existing reviewed rollout ID**:

```sh
herdr-eng fleet rollout run --id ROLLOUT_ID --interval-seconds 30
# Optional finite run, useful for canaries or a supervised batch:
herdr-eng fleet rollout run --id ROLLOUT_ID --interval-seconds 30 --max-passes 10
```

`run` repeats reconciliation until complete or operator attention is required.
Offline workstations retry the same release automatically when they reconnect.
Unchanged evidence doubles the delay from 30 seconds up to five minutes; actual
progress resets it. The interval accepts 1–3600 seconds, and the delay cap is the
larger of the interval and 300 seconds. Attempts and replacement busy-operation
IDs do not count as progress. `--max-passes` limits passes for this invocation.

A dedicated per-rollout lock prevents two unattended controllers, including while
one sleeps. `status` exposes the saved runner status, total pass count, last pass,
next retry and stop reason without probing or modifying anything. Ctrl-C records
`interrupted` and releases the lock. Restarting the same command and state directory
retains release selection, operation IDs and scheduled backoff. The command can
run under an existing process supervisor; it does not install or enable a service.

Bootstrap requirements, missing identity/coverage, rejected activation and unknown
failures stop the automatic controller with `runner.status=blocked` and a reason.
Correct the reported issue and explicitly rerun the command. Recognized offline
transport failures keep retrying with bounded backoff. This distinguishes waiting
for a returning workstation from repeating an unclassified failure forever.

Exit code 0 means all recorded hosts and sessions verified; code 2 means pending,
blocked or max-passes work remains; code 1 indicates an invalid plan or local
execution error. Ctrl-C exits 130 after persisting state. `status` remains read-only.

Per-host work is serial so one rollout does not create a fleet build storm.
Transport calls have timeouts; a stopped controller resumes from durable state.
Requests are saved before sending and receipts are saved atomically with fsync.
Typed failed receipts emitted with CLI exit code 1 are recorded as failures. A
skipped `busy` outcome or `STALE_MISSION_FENCE` code proves no mutation occurred:
its receipt is archived and the next pass can create one fresh request using the
latest mission fences. Other skipped outcomes remain explicitly blocked until
refreshed state proves operator intent or a terminal mission; they do not generate
unbounded fresh recovery operations.
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
reference that matches one reachable control descriptor. When the native reference
is absent or contains a custom transcript filename without a conversation ID,
the controller can instead read that pane's foreground processes on the
same host and require exactly one matching live Pi PID. The control response must
attest the descriptor's PID, conversation and instance; a fresh native snapshot
must retain the same pane, terminal, revision/state sequence and native reference.
Custom transcript files are never opened or inferred from their names. The proof is
recorded separately as `verified_foreground_pid`, without modifying native identity.

Duplicate conversations, ambiguous foreground processes, stale/unreachable control
instances, reused panes and PID changes block activation. Terminal titles, working
directories and recent session files are never identity evidence. An agent whose
identity cannot be proved remains a coverage error while independently identified
siblings continue; the host cannot be reported complete.

Already-current settled runtimes are inspected without preparation or draining, so
idle, paused and terminal sessions keep accepting future work. The coordinator
verifies both the loaded runtime commit and the stable Host commit
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
coverage, corrupt receipts, progress regression and expired waits. Runner tests
cover reconnect, durable restart/backoff, interruption, duplicate controllers and
explicit operator-attention stops. These tests do
not constitute a live multi-workstation rollout; validate a canary before fleet
activation.
