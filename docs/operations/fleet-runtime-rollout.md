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
# Repeat --host to plan several hosts in one rollout (a duplicate is rejected);
# omit it to select every host in the private fleet inventory.
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

Online sessions have a separate durable no-progress ceiling: after **8 unchanged
online passes following the initial observation**, the controller stops with
`runner.status=stalled` and identifies the session requiring review. This covers
permanently busy preparation and resumed missions that never produce useful work.
The counter is per session, so another agent's progress cannot hide a stall, and
controller restart does not reset it. The mission is neither canceled nor killed.
`--max-unchanged-online-passes N` configures the threshold from 1 to 1000 (default 8).
Offline observations do not consume or reset the budget; offline-only rollouts can
keep retrying until reconnection. Verified named future waits remain accepted.
After correcting the reported cause, rerun the same pinned rollout; fresh meaningful
evidence resets that session's budget.

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
blocked, stalled or max-passes work remains; code 1 indicates an invalid plan or local
execution error. Ctrl-C exits 130 after persisting state. `status` remains read-only.

Per-host work is serial so one rollout does not create a fleet build storm.
Transport calls have timeouts; a stopped controller resumes from durable state.
Requests are saved before sending and receipts are saved atomically with fsync.
Typed failed receipts emitted with CLI exit code 1 are recorded as failures. A
skipped `busy` outcome, `STALE_MISSION_FENCE` code, or `INTERACTIVE_BUSY` code
proves no mutation occurred: the receipt is archived and the next pass can create
one fresh request using the latest mission fences. The Pi runtime reports its
not-yet conditions as `busy` (runtime not ready, mission planning or recovering,
custody changed, no local resumable controller) and a busy interactive session as
`INTERACTIVE_BUSY`; both are time, not operator attention, so they retry instead
of blocking the rollout. Other skipped outcomes remain explicitly blocked until
refreshed state proves operator intent or a terminal mission; they do not generate
unbounded fresh recovery operations.

A session whose runtime activated but whose stable Host still runs an older
release records `bootstrap_required` with a durable `nextStep`: restart the stable
Pi Host on that host so it loads the selected release, then resume the rollout.
A hot reload never changes `hostCommit`, so no pass can fix this by itself.
Uncertain deliveries reuse the original operation ID and payload. A replacement
runtime instance in the same process polls the same durable receipt.

The default state directory is `~/.local/state/herdr-engineering/rollouts`, mode
0700, with 0600 JSON files. Use the same directory after restarting the controller.
Do not edit it to change a revision, destination, process, or operation ID. Create
a separate rollout for another release. There is no automatic downgrade or
cross-rollout rollback: a rollback is a separately reviewed pinned release plan.

## Unattended convergence: pin, resume, prune

Plans are only as good as the revision they target and the driver that drains
them. Three mechanisms keep the fleet on one target (#844):

**Pin the target.** Write the full 40-character commit SHA you want the fleet's
Pi runtimes on, one line, to `~/.config/herdr-engineering/pi-rollout-revision`
(the `HERDR_PI_ROLLOUT_REVISION` environment variable overrides the file). With a
pin, `fleet rollout plan` no longer requires `--pi-revision`; an explicit flag
still wins. Without a pin, planning without `--pi-revision` fails closed, and the
unattended driver does not touch rollouts at all. An invalid pin (not a full
lowercase 40-hex SHA) is a failure, never a silent fallback.

**Resume, don't re-mint.** `fleet rollout latest` prints the newest plan that has
not settled (`complete` or `abandoned`), or `{"id": null}`. The daily controller
(`herdr-eng-fleet-update.timer` -> `herdr_engineering.fleet_autoupdate`) runs this
selector after each successful controller update: with a pin it resumes the newest
unfinished rollout with a bounded pass window (`--max-passes 3`, 30-second
interval), and mints a new plan only when nothing unfinished exists **and** the
pinned revision has never been rolled out - once a plan for the pin settles, later
nights report a skip instead of re-staging the converged fleet, and an abandoned
plan for the pin is respected as the operator's decision to retire it. A plan
whose revision does not match the pin is skipped and reported in the run record -
the operator settles it with `fleet rollout abandon --id ROLLOUT_ID --reason ...`
(settling a `complete` rollout is refused) or finishes it. The result of the pi
step is recorded in the unattended run state next to the controller update, so a
permanently skipped or blocked drain is visible without reading the journal. The
step's own failures - a malformed pin file, an unreadable store, a rollout lock
held by an operator command - are recorded as `pi_rollout.status: error` (or
`prunedError` beside a completed drain) and never undo a successful controller
update: the run's pass/fail verdict belongs to the controller update.

**Bound the state.** `fleet rollout prune [--older-than-days N]` (default 30,
bounded to 0-3650: a negative horizon would delete every settled record) deletes
settled records - `complete` or `abandoned`, judged by their last pass or creation
time - so the state directory reflects current intent. Unsettled records are never
touched: a blocked rollout may still be the operator's next action, and only a
deliberate `abandon` marks it done - and like `apply` and `run`, `retire-identityless`
refuses an abandoned rollout rather than silently reopening it. The daily driver
prunes after each drain.

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
  code differs from the target. Runtime hot reload cannot replace that stable
  Host. Preserve its conversation and work, then perform a controlled extension or
  process bootstrap before a new reconciliation plan. This command intentionally
  provides no forced kill/restart fallback.

  A hot reload never changes the session's reported `hostCommit`, so a differing
  commit alone does not prove stale Host code. When `hostCommit` differs from the
  target, the coordinator runs `git diff --quiet <hostCommit> <revision> -- <Host paths>`
  in the staged release on the target host (same transport as `sessions list`, with
  `GIT_DIR`, `GIT_WORK_TREE` and `GIT_COMMON_DIR` unset). No change means the Host is
  accepted by code equivalence and the session continues; its state records
  `hostAcceptance` with `mode: "code-equivalence"`, both commits and the paths
  compared. Any change, or any inability to compare (release without `.git`, either
  commit unavailable, git or transport error), keeps `bootstrap_required`. The Host
  paths are the fixed list `HOST_CODE_PATHS` in `herdr_engineering/fleet_rollout.py`
  (`src/runtime/host/` and the rest of the stable Host's import closure). `package.json`
  is compared parsed through `git show`, ignoring only `scripts` and `devDependencies`
  (the Host never reads them); `hostAcceptance.packageJsonIgnoredKeys` records that.
  An unreadable or unparsable `package.json` keeps `bootstrap_required`. Staged releases are depth-1 clones, so a missing old `hostCommit` is
  fetched without network access from the sibling release
  `pi/releases/<hostCommit>` when that directory still exists; otherwise the
  session stays `bootstrap_required`.
- `blocked`: identity, protocol, revision, or coverage evidence was rejected.
  Inspect the per-host error and per-session receipt. An uncertain request is never
  replaced with a fresh resume against a different mission revision.

Existing sessions without the new control API need this explicit first bootstrap.
An unreachable machine remains pending on the original immutable release. Operator
pauses survive maintenance and do not count as a mission recovery failure.

## Registrations with no instance identity

A Pi registration can exist and be reachable while carrying no `instanceId` — a pane whose
session ended but whose metadata outlived it. The rollout refuses to act on it, which is
correct: there is no instance to send a prepare or reload to. The refusal is also permanent,
so `--max-passes` repeats the same blocked host forever and the release can never reach
`complete`. An operator settles it explicitly:

```bash
herdr-eng fleet rollout retire-identityless --id <rollout-id>
```

Only sessions blocked on a **missing** identity are retired. A session blocked because two
live registrations claim one conversation stays blocked — that is a real disagreement, not a
stale husk, and nothing here settles it. Retired sessions are recorded per host in
`retiredSessions` (sessionId, pid, terminal, the refusal, when, who settled it), stay visible
in every later report, and appear in `coverageErrors` while the husk is still present. If the
identity ever resolves, the next pass picks the session back up and stamps `unretiredAt` — a
wrong retirement is corrected rather than carried forever.

Retirement settles only the identity refusal. A host blocked for a host-level reason
(unverified native coverage, ambiguous native identity, a missing discovered session) stays
blocked with that reason in `error`/`coverageErrors` and keeps the plan from completing. A retired
session is terminal for the runner and keeps its refusal in `error`; it vanishing from the native
inventory is not a blocker. The command exits 2 while `stillBlocked` sessions or `blockedHosts` remain. Run `apply` first on plans written before this change, so sessions carry the typed `blockReason` and hosts carry `hostLevelError`.

## Unresponsive and exited sessions

`sessions list` waits a bounded time per session. On a loaded controller every live session can
miss its reply in the same pass (rows come back `reachable:false`, `error:"session_unresponsive"`),
which says nothing about the sessions. A session with no reachable row is classified by its rows:

| Rows for the session | Outcome |
| --- | --- |
| 2 or more `reachable:true` | `blocked` (`session identity missing or ambiguous`), unchanged |
| 0 reachable, at least one unreachable with a non-terminal error (`session_unresponsive`, a timeout, no error) | transient: re-list up to twice in the pass (0.5 s, then 1 s backoff); if still unreachable the session **keeps its previous status and error** and is retried next pass. A `complete` session stays `complete`; a new one becomes `pending`. `lastUnreachableAt` and `note` record the miss. Never blocks. |
| every row `session_process_gone`, or no rows and the session left the native inventory | `ended`: the process exited and there is nothing to update. |

`ended` is terminal and non-failing, like `retired`: it does not block the host, does not hold
the rollout open, and does not consume the online stall budget. It is never a success. The host
lists such sessions in `endedSessions`, and the session keeps `endedAt` and `note`, so a report
can tell "updated" from "exited". If the same conversation answers again (possibly in a new process on an old runtime), the
next pass restarts it as a fresh `discovered` session: prepare, reload and resume receipts,
`prepared`, `loaded`, `missions` and `hostAcceptance` from the old process are discarded and
only `previousIncarnations` (pid, instanceId, endedAt, last status) is kept for the report. A discovered session that is unreachable (not exited) and
absent from the native inventory is treated as transient, not as a missing-session blocker.

## Correctness rules

- **Offline detection** uses the staging return code (4, 124, 255) and whole ssh transport
  phrases (`Network is unreachable`, `Host is unreachable`, `No route to host`,
  `Connection timed out`, ...) in the error text the controller recorded. Playbook output is
  never searched, because every Ansible recap contains `unreachable=0`. A dirty-release block
  (`blockCode: managed_release_dirty`) is never classified offline: its text carries remote
  file names.
- **Stale herdr registration.** When a pane's native conversation id names a registration with
  no reachable row (for example a dead predecessor whose row reports `session_process_gone`),
  the live Pi in that pane is identified by its foreground PID before anything settles. Only
  "no Pi process runs in the pane" (an empty pane, or only a shell) lets the registration be
  treated as a husk and ended. If the pane cannot be proved (invalid pane or terminal id, no
  revision token, a failing process query, a live Pi whose control row did not answer) the
  session stays `pending` with a `lastUnreachableAt` note and is retried; it never ends and the
  host never completes. A registration that is only transiently unreachable keeps its previous
  status without any remote pane query. A host is never marked `complete` while a live Pi
  session in a pane was not reloaded.
- **No Pi sessions.** A host with no native Pi sessions, nothing tracked, and no reachable Pi
  control row is settled `complete` with
  `statusReason: no Pi sessions on host; release staged, nothing to reload`. If a Pi control
  row answers although herdr lists no Pi (a Pi outside a pane, or one herdr never tagged), the
  host stays `blocked` with `no native Pi sessions; expected coverage unverified`.
- **Hosts added later.** The unattended step mints a delta plan for the hosts no settled plan
  (complete or abandoned) for the pinned revision ever named, whatever the newest plan's
  status; hosts of an abandoned plan stay retired. Pruning keeps, for the pinned revision, the
  settled plans that together record that coverage, however old, so a small delta plan can
  never make the whole fleet look uncovered. `fleet rollout prune` protects the current pin
  the same way.
- **State directory.** An existing rollout state directory owned by you that is group or
  world readable is tightened to `0700` by commands that write (apply, run, abandon, prune,
  plan). Read-only commands (`status`, `latest`) never change permissions; symlinks and
  foreign owners are refused on every path.
- **Dirty managed release.** If the staged release directory has local changes, staging fails
  with a typed `managed_release_dirty` error. The host is `blocked` with
  `managed release is dirty: <files>; local work preserved` and a `nextStep` naming the
  release directory. Local work is never reset; clean or move it, then re-run apply. Files
  come from `git status --porcelain=v2 -z` (names with spaces or quotes intact, renames shown
  as `old -> new`), the list is capped at 20 with an explicit `(+N more)` note, and all
  remote-derived text is stripped of control characters and ANSI escapes before it is stored
  or printed.

## Validation coverage

`tests/test_fleet_rollout.py` exercises real durable state and the production JSON
transport through a subprocess fixture: two hosts, offline recovery, controller
restart and lost replies, exact replay, old loaded code, stable Host mismatch,
operator pause, busy preparation, duplicate/stale identities, missing native
coverage, corrupt receipts, progress regression and expired waits. Identity-less
registrations are covered for the refusal, operator retirement, the evidence kept in the
report, ambiguous identity refusing to be retired, and a retired session rejoining when its
identity resolves. Transient unresponsive rows, ambiguous rows, exited sessions and the in-pass
retry are covered with a subprocess runner that returns different lists on successive calls. Runner tests
cover reconnect, durable restart/backoff, interruption, duplicate controllers and
explicit operator-attention stops. These tests do
not constitute a live multi-workstation rollout; validate a canary before fleet
activation.
