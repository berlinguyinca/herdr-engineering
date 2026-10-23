---
name: pi-recovery
description: Use when the operator requests recovery of stalled Pi sessions through HerdR after an inference outage, or wants a repeatable fleet recovery plan.
---

# Pi outage recovery

Run from a HerdR-managed pane on the fleet controller. Read `herdr --skill`
and confirm `HERDR_ENV=1`; never manufacture caller context. The installed
`herdr-pi-recover --help` is the isolated companion command authority. In a
source installation, `herdr-eng fleet recover` exposes the same interface.

1. Preview: `herdr-pi-recover plan --inventory /absolute/path/hosts.yml`.
   Optional `--host HOST` and `--session NAME` select explicit inventory/native
   targets. Preview reads native inventories and writes a private local plan;
   it never prompts or changes a Pi session.
2. Inspect `sessions`: `eligible` has a proven native foreground PID, process
   birth, exact live Pi conversation and current inference-error evidence.
   Active, ambiguous, paused, cancelled and completed work is excluded.
3. When recovery is authorized, run `herdr-pi-recover apply --id ID`.
   Apply tests compatible models using a real tool-call completion, selects an
   exact model, verifies the acknowledgement and transcript, and continues the
   existing work. A proven model picker is escaped once; SDK `/reload` refreshes
   the registry while preserving the original flags and tools, then the exact
   model is selected again. Any ambiguous acknowledgement stops recovery. One controller-wide lock serializes all runs; every attempted
   session reserves a durable minimum 60-second cooldown **after completion**.
   A crashed controller adds a fresh 60-second cooldown on the next apply.
4. For an already authorized bounded batch, `herdr-pi-recover run` combines
   plan and apply and prints the plan ID before attempting recovery. The default
   command remains preview-only. The menu's **Recover stalled Pi sessions** action
   invokes this explicit run.
5. Read `herdr-pi-recover status --id ID`. Status verifies up to ten oldest
   interactive entries, starting checks within a 60-second budget, and stores
   content-free evidence locally. `progress_verified` requires a new completed
   assistant turn or successful tool result descending from the recorded baseline
   under the exact live session/process identity; it does not mean the task is
   complete. Earlier successful work still counts while the next tool is pending;
   a newer inference error overrides that success until later progress is proven. `inference_failed`, `verification_waiting`, and `verification_unknown`
   distinguish errors, absent progress, and missing proof. Native auto-fallback is
   reported with the actual compatible gateway model and `modelChanged`, checked
   against the original requirements or a conservative recorded candidate envelope;
   absent original requirements leave fallback progress unverified. Status never
   falsely confirms the originally selected model.

   Status never prompts, reloads, resumes, or probes a model. It may read an existing
   pending maintenance operation by its durable ID; a successful reload receipt
   remains separate from work progress and never clears the no-replay journal.
   Typed mission progress remains pending separate authoritative verification.
   During an active recovery batch, status returns the ledger with verification
   deferred. Repeat status to cover the next oldest entries. A timeout never
   justifies replay; a second plan cannot replay the same failed turn.

Use the same controller and default state directory for all operators. This is
not a distributed lock across independently operated fleet controllers. Do not
manually erase the journal to force retries. Do not run two independent manual
and automatic recovery loops concurrently.

## Bootstrap and unsupported cases

To upgrade old handlers within the same paced recovery operation, plan/run with
`--pi-revision FULL_REVIEWED_COMMIT`. Apply stages that exact immutable release
once per host through the existing fleet staging path, then journals typed
prepare/reload, verifies same-process custody and the new handler before resume.
The native owned-mission revision/generation and current UI are rechecked before
each mutation; pauses, completed work and missing outage evidence are excluded
before considering an upgrade. A running native operation is polled by its durable
operation ID; a lost response is never permission to repeat the action.

`bootstrap_required` means the process loaded an older runtime than its installed
release. Use existing `herdr-eng fleet rollout` maintenance first, or a separately
supervised SDK `/reload` for an idle interactive session. After changing runtime,
create a fresh recovery plan. An unavailable
owned-mission inventory remains excluded: ping liveness or control-plane metadata
does not establish mission ownership. No automatic process kill is performed.

A model picker, approval dialog, missing exact model acknowledgement, changed
process or changed conversation stops that attempt. Inspect before intervening.
Interactive recovery only handles a current inference error, not user-aborted or
already completed turns. An empty editor must be positively visible and no current
Working/Thinking/Waiting-for-model indicator may be present, even if native status
says idle. Historical tool calls removed by native compaction/context edits are
classified through Pi's canonical in-memory session projection only when the
installed launcher/CLI/SDK ownership and version, process age, active parent chain,
and unchanged transcript digest are proven. The helper has read-only filesystem
permissions and never opens a persistent session. Malformed historical records
must have identities proven outside the active chain; missing older parents need
a validated compaction checkpoint. Unavailable SDK proof, unresolved projected
tool calls and current error-turn calls remain excluded. Actual assistant model
metadata supersedes older restored aliases; a newer explicit model change wins. Missing model capability metadata is not permission to
fall back from vision or reduce context. Provider credentials stay on each host.

Mission recovery requires the loaded runtime capability
`mission.resume.selected-model.v1` and its positive `interactiveIdle` attestation.
Only a single owned, explicitly eligible, unpaused mission with current inference
outage evidence is resumed. The typed request includes its revision/generation
fences and the live-tested exact provider/model; its receipt must acknowledge the
same selection. Older runtimes are excluded before any model change. Never use
`/mission resume ID --model EXACT`: deployed slash syntax only supports `auto`.
Never unpin unrelated missions globally.

## HerdR menu

The companion `deploy/herdr-pi-recovery-plugin` contributes **Preview Pi recovery**
and **Recover stalled Pi sessions** to the global menu. Link it through the installed native plugin interface; no
server replacement or restart is needed. Preview only creates a plan; Recover runs a bounded batch with the same checks,
journal and cooldown. Configure the isolated companion as described in the
plugin README; leave the shared HerdR Python environment and services untouched.
