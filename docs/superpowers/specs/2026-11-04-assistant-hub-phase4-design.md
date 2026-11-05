# Assistant hub Phase 4: ask me only when it matters

Status: draft for owner review (2026-11-04). Builds on Phases 1-3 (PR #537, #541, #608).
Facts about today's behaviour come from `docs/operations/assistant-hub.md` and `deploy/control-plane/deploy.sh`.

## Intent

The owner gives a coding worker a request and walks away. The hub lets routine work run, and contacts the owner in
Slack only when one of three things is true:

1. a worker has a **real question** the hub cannot answer itself,
2. a worker wants to do something **dangerous that was not allowed before**,
3. a worker wants to **deploy** and that project and scope is not yet set to deploy automatically.

Everything else runs quietly and ends with one summary. Later the owner wants **automatic deploys**, decided per
project and per scope. The first project is herdr-engineering itself.

Why now: Phase 3 made autonomy possible but gave the owner no practical way to answer asks, build up trust, or
see what ran. The only owner surface today is the operator REST API and CLI.

## Goals

- **Slack as the owner's channel.** One message per ask; Approve, Deny, and "Approve and remember" taps; threaded
  replies for questions.
- **Approve-and-remember.** A tap can create or advance a standing grant for that project and scope, so the hub asks
  less over time without a separate activation ceremony.
- **Per-project, per-scope deploy permission.** Default is ask. Automatic is an explicit owner choice for one
  project and one scope.
- **One summary at the end** of a task (and nothing in between for quiet work).
- **Safety stays where it is.** Vetoes, the fail-closed judge, the audit chain and the persisted-state trust
  boundary keep working unchanged.

## Non-goals (deferred)

- **Blue/green deployment.** A separate follow-up project with its own design (shared Postgres migrations must be
  compatible with both versions, the worker must not run twice, the edge needs a traffic switch). Phase 4 calls
  whatever deploy command exists today (`deploy/control-plane/deploy.sh`) and gets blue/green later without hub changes.
- Hook-attested evidence, Codex/OpenCode/Pi harness adapters, the canary prompt, email/spec ingestion.
- Auto-deploy for any project other than herdr-engineering. The config is generic; only that project is configured.
- Multiple owners or Slack workspaces.

## Anti-goals (a failure even if it works)

- The hub says `authorized` for a dangerous or deploy action without the owner's explicit tap or a standing permission the owner set.
- Slack text, emoji or thread replies authorize anything. Only a verified button tap does.
- The owner is asked about something a veto already denies, or is spammed with asks for routine work.
- A deploy breaks the control plane and nobody is told.

## How it behaves

### 1. Asks reach the owner in Slack

When `action_request` comes back `pending` for a reason the owner can resolve (unknown or unauthorized action,
judge escalation, deploy not on auto), the hub posts one Slack direct message to the owner: what the worker wants
(the masked `display` text already used on the owner routes), which worker and project, why it is asking
(`source`: no grant, judge escalated, deploy default), and three buttons: **Approve**, **Deny**, **Approve and remember**.
Vetoed actions are never offered; the worker gets `denied` and the owner gets no ask.

Approval is recorded as an owner decision through the same code path as the REST route, with a new provenance
`owner_slack` that counts as owner evidence exactly like `owner_api` (this widens the owner trust boundary to the
owner's verified Slack account; accepted by the owner on 2026-11-04). A worker is never told about the Slack channel.

Guard rails (the owner's call that Slack taps are enough, plus these):
- The tap must come from the configured owner Slack user ID, on a message the hub posted, carry arrive over the authenticated Slack connection, and name one request ID plus a one-time nonce.
- An approval applies only to the request it names and expires (default 15 minutes; then the request stays `pending`).
- Reposting, editing or forwarding a message cannot approve anything; a second tap on a decided request is refused.
- The hub marks the message as decided (buttons removed, outcome shown) so stale taps are visible.

### 2. Real questions

Every `assistant_ask` question is posted as a Slack message in a thread (there is no answering from memory in this phase; asks are rate-limited and batched per task so one chatty worker cannot spam); the owner's
threaded text reply is returned to the worker as the answer (text replies are data for the worker, never authorization).

### 3. Approve and remember

"Approve and remember" records the approval as evidence and, in the same tap, confirms activation of the resulting
grant for that exact action key, tenant, user, worker and scope as soon as the existing gates allow it (global
shadow period, evidence thresholds, judge gate, `still_in_shadow`, `judge_*` codes). If a gate refuses, the hub tells the owner
in the thread what is still missing instead of failing silently. Nothing about the gates is relaxed for ordinary actions.

### 4. Deploy permission, per project and scope

New config block under `personal_assistant` (names are the builder's call, shape is fixed):

```
deploy:
  projects:
    herdr-engineering:           # project id
      repo: berlinguyinca/herdr-engineering
      scopes:
        control-plane:           # scope id
          mode: ask              # ask (default) | auto
          command: deploy/control-plane/deploy.sh
          require: [on_main, ci_green, descends_from_deployed]
```

- `mode: auto` for a project and scope is set by the owner, with a Slack confirmation tap, and is revocable at any time.
- Auto-deploy needs every precondition in `require` verified **by the hub**, not claimed by the worker: the target
  commit is the tip of main (GitHub, via the existing `github.app` config); its CI checks are green (GitHub status plus
  Woodpecker's unauthenticated read endpoints); and it descends from the deployed commit, which the hub reads from
  `/api/v1/health` (matches `deploy.sh`'s own rule). The hub does not run `deploy.sh --dry-run`; `deploy.sh` validates
  everything before it changes the host, and `tests/test_deploy_scripts.py` runs in `unit-gates`. (Builder's call to revisit.)
- **Project and scope are decided by the hub, never the worker.** A request maps to a project and scope only when its
  canonical argv equals the configured `command` with `--ref <full sha>` and the worker's `repo` hint agrees with the
  configured repo (a hint can only make this stricter, per the hint trust rule). Anything else is an ordinary unknown action.
- **Standing permission, not a bypass.** `mode: auto` stores a standing grant (source `owner_standing`, set by the Slack
  confirmation tap) for the deploy verb and scope. It skips only the approval-count and session/task diversity
  rules; `effective_state`, the global shadow period, the judge and vetoes all still apply.
- If any precondition fails or cannot be checked, the result is `pending` and the owner is asked.
- Vetoes, the judge and the global shadow period still apply. The judge can still escalate a deploy to the owner.
- The authorization binds one commit: the argv must carry `--ref <full sha>` and the hub checks that sha. A command with
  any other argv is a different key and is not authorized.
- **The hub is advisory, as in Phase 2.** A worker asks voluntarily; the hub cannot stop a harness from running a command it
  was not authorized for. What it guarantees is that it never says `authorized` for it and that it checks the outcome
  afterwards (section 5). Enforcement at the harness (hooks) is deferred with hook-attested evidence.

### 5. After a deploy

The worker runs the deploy itself; the hub only authorizes it (the worker's process is outside the control-plane
containers, so restarting them does not kill it). The hub then **independently verifies the outcome**: it persists a
"deploy check" record, and after it comes back up (it will be briefly down when its own container is recreated) it
polls `/api/v1/health` until the target commit is reported or a deadline passes, then posts the result to Slack:
healthy; rolled back and healthy (`deploy.sh` exit 1); or **failed to roll back** (exit 3), which is an urgent
ask that is never suppressed. A deploy whose outcome cannot be confirmed is reported as unconfirmed, not as success.

### 6. End-of-work summary

When a worker's `report` for a `task_id` is marked final (or the task has been idle 30 minutes, my call), the hub posts one Slack message: what it did, which actions ran quietly under
which grants, anything denied or vetoed, and any deploy outcome. No other messages are sent for quiet work.

## Constraints

- The hub is tailnet-only and has no public inbound endpoint, so Slack interactivity must not require one
  (builder's call; Socket Mode is the expected fit). Socket Mode has no per-request signing secret: the app-level token
  authenticates the connection, and the hub additionally checks the Slack user ID and the one-time nonce. The Slack bot token and signing secret are stored like the judge
  key: registered via a CLI that reads stdin only, file mode 0600, never logged, never in the audit chain.
- Messages never contain secrets; they reuse the masked `display` and the existing "treat as sensitive" caveat.
- No mocks in tests; Slack calls are tested against a real local listener, as the judge client is.
- Existing audit rules continue: new audit actions for Slack ask, Slack decision, deploy authorization and deploy outcome;
  entries carry ids, not command text.
- Existing rules for branches, TDD and CI are unchanged.

## Order of work

1. Slack ask/answer channel and `owner_slack` provenance (usable alone, even with `mode: ask` everywhere).
2. Approve-and-remember and the end-of-work summary.
3. Deploy projects/scopes config, hub-side precondition checks, post-deploy verification.

## Honest limits

- Autonomy still waits on the owner registering the Jev key and `backend_confirmed`, the 14-day global shadow
  period, and the gate's label minimums (about 200 labeled decisions per risk class). Slack taps make labels cheap to
  collect, but they do not remove these requirements.
- Anyone who can tap as the owner on Slack, or who holds the Slack token, can approve. This was accepted by the owner.
- The hub cannot see a worker's actions that happen outside `action_request`.

## Calls the design makes for the builder

- Pending asks, nonces, expiry and deploy-check records live in the existing assistant store snapshot (same trust boundary
  as grants), so they survive hub restarts and a mid-deploy recreate; the deploy check resumes on boot.
- If an "Approve and remember" tap meets a refusing gate, the approval is still recorded and the hub says in the thread
  what is missing; the remember intent is kept and retried at the next qualifying approval, not by polling.
- The Slack owner user ID is set at registration of the Slack token (CLI, stdin only).

## Left to the builder

Slack app manifest and library, message layout, exact config key names, the deadline and polling interval for the
post-deploy check, how a worker learns a pending ask was decided (re-asking the same request is the expected shape),
and test layout.

## Decisions confirmed by the owner (2026-11-04)

- **Deploy trust model:** for the deploy scope only, an explicit owner opt-in (`mode: auto` plus a Slack confirmation tap)
  replaces the approval count. Vetoes, the judge, the shadow period and the hub-verified preconditions still apply.
- **Advisory enforcement is acceptable** for the first auto-deploy: the hub never says `authorized` for an unmatched command
  and verifies the outcome afterwards; harness-level enforcement is deferred.
