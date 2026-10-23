# Automated PR review and merge gate

Status: draft for approval. Nothing here is built yet.
Repos: `herdr-engineering` (control plane, owns the pipeline), `pi-engineering` (reuses its merge-gate decision code and its pi-mission agents).

## Intent and the why

The user is mostly a single developer. Today they wait for CI to finish, then tell the authoring agent "now review it", then relay the findings back, then merge. They are the only link between CI, review, fixing and merging. The goal is to remove that link: a PR opened by an agent is tested, reviewed by a different hosted model, fixed by its author agent, re-tested and re-reviewed in a loop, and merged without any human click. LLM cycles must not be wasted, so review only ever runs on code whose tests have already passed.

## Goals

1. When the PR-level tests for a head SHA finish green, a review job is queued automatically.
2. A pool of N reviewer workers (hosted models) drains the queue; N scales with queue depth.
3. The reviewer returns a structured list of required fixes (or approval), pinned to the head SHA it reviewed.
4. The author agent is notified with that list and fixes the PR. CI re-runs, then review re-runs, until approval.
5. The merge gate is a GitHub required status check set only by the pipeline. When it is green, the PR merges by itself.
6. After merge, the full test suite runs on main. A failure notifies the author agent and the user.
7. Latency matters: reviewers and the gate must never be the bottleneck.

## Non-goals

- No human approval step for normal PRs.
- No review of a PR whose PR-level tests are red or still running.
- No automatic revert of a merged PR (the user chose notify-only).
- No universal affected-test selector. How a repo picks its PR-level tests is per-repo configuration.
- No new CI system. Woodpecker stays the execution authority.

## Anti-goals (failure even if it technically works)

- A fix/review loop that never ends or burns tokens indefinitely.
- A review or approval applied to a SHA other than the one it examined.
- A review spent on a commit that was superseded by a newer push.
- The merge gate being satisfiable by the authoring agent or by anything except the pipeline.
- Silent test skips counting as passing on main.

## Decisions made by the user

- Merge happens automatically once CI-green and review-approved.
- "Green" for a PR means the new or affected tests pass. The complete suite must pass only on main, or be explicitly disabled. Some projects have suites that take weeks.
- A full-suite failure on main notifies both the author agent and the user. It does not revert.
- Review starts only after all PR-level test runs have finished.
- Queue plus N reviewers, scaled by load.

## Design

### Pipeline states per (repo, PR, head SHA)

`ci_pending` -> `ci_green` -> `review_queued` -> `reviewing` -> `changes_requested` | `approved` -> `merged`.
Terminal side states: `ci_red` (back to author, no review), `superseded` (a newer SHA arrived), `escalated` (round cap hit).
All state is keyed by head SHA. A push creates a new SHA and supersedes everything pending on the old one, including cancelling an in-flight review.

### Components

1. **Trigger.** Consumes the existing `CIEvent` stream from the Woodpecker connector. When every pipeline Woodpecker reports for the head SHA has finished and the required PR-level pipelines are success, emit `ci_green`. Any failure emits `ci_red` and notifies the author agent with the CI failure; no review is queued.
2. **Review queue.** Durable (in the control plane's PostgreSQL event log), ordered by enqueue time, one live job per (repo, PR). A re-queue for a new SHA replaces the old job. Dedupes on (repo, PR, SHA).
3. **Reviewer pool.** Workers are agent sessions started the same way `collab-spawn.py` starts agents (herdr workspace per worker, round robin over enabled hosts). The reviewer model is never the model that authored the PR (separation of duties, reusing the rule from pi-engineering's lifecycle). Pool size scales between a min and max with queue depth; idle workers are retired. Models come from the hosted fleet.
4. **Review output.** The reviewer must produce a structured verdict: PASS, or FAIL with findings (file, line, severity, required change). Findings are fingerprinted so a later round can tell new findings from unchanged ones.
5. **Merge gate.** Reuses the pure `decideMerge` logic from `pi-engineering/src/merge/mergeGate.ts`: CI results and the review verdict are accepted only if their SHA equals the PR head. The result is published to GitHub as a commit status (for example `herdr/review-gate`) that branch protection marks required. Only the pipeline's credential can set it. On `merge`, the pipeline merges the PR through the GitHub API.
6. **Author notification.** Findings or CI failures are delivered to the agent that opened the PR, found through its session link recorded at spawn (`pane:<host>:<pane>` for herdr/collab agents; the mission for pi agents). If the author session is gone, the pipeline spawns a fresh fixer agent with the PR, the findings and the branch.
7. **Round cap and escalation.** A PR may go through at most R review rounds (default 4, configurable). On the cap, or if the same finding fingerprint is returned unchanged after a fix attempt, the PR is marked `escalated`, the gate stays red, and the user is notified. Escalation is the only point the user is pulled in during a PR's life.
8. **Main-branch verification.** After merge, the full suite runs on main in Woodpecker (may take very long; it never blocks merges). Disabled tests must be listed in an explicit, versioned per-repo file; a skip anywhere else counts as a failure. On failure, notify the author agent and the user, naming the PR whose merge most likely caused it (first merged commit after the last green main run).
9. **Dashboard.** A native page, in the style of the CI and Reviews pages, shows the queue, reviewers (busy/idle, model), and each PR's state and round count.

### Per-repo configuration

Required PR-level pipelines/checks; how affected tests are selected; the main-branch full-suite pipeline; the disabled-tests file path; round cap; whether the repo is enrolled at all.

### Constraints

- GitHub is the merge target; branch protection must require the gate status and strict (up-to-date) checks. `pi-engineering/src/merge/protectionDrift.ts` already detects weakened protection and should be run against enrolled repos.
- Woodpecker is truth for CI status; stale or missing data is "not green" (per the CIEvent contract), never inferred success.
- Reviewer and author sessions run text-capable agents inside the existing sandbox rules (bubblewrap, audit log). A reviewer gets read access to the PR; it does not get merge or push rights.
- Model cost: one review per (PR, SHA) at most; cancel superseded work immediately.

## Decided by me (not confirmed by the user)

- Both agent families (herdr/collab agents and pi missions) are supported as authors, since the user did not say which they use.
- Default round cap of 4; escalation on repeated identical findings.
- Pool defaults and scaling thresholds are left to configuration with conservative defaults.
- The gate is a GitHub commit status plus API merge, not GitHub's native merge queue.
- Review jobs are stored in the existing PostgreSQL event log, not a new broker.

## Decided by the user after the builder check

- **Scope: every PR in an enrolled repo**, including PRs the user opens by hand or that come from unmanaged agents. This requires an **author registry**: a durable map from (repo, PR) to the author session (host, pane or mission) and the authoring model, written when an agent is spawned or opens a PR. A PR with no registry entry, or whose author session is gone, is fixed by a **fresh fixer agent** the pipeline starts with the PR branch, the findings and the CI logs. The fixer's model may be any hosted model; the reviewer must still differ from whichever model wrote the code being reviewed (for a hand-written PR, any reviewer is acceptable). The registry is a prerequisite slice, built first.
- **Credentials: a dedicated GitHub App** for the pipeline sets the gate status and merges. Agents get tokens that can push branches and open PRs but cannot set commit statuses or merge. PATs are minimised: the goal is the App plus the minimum agent-scoped credentials, and no shared personal token.

## Further calls by me after the builder check (not confirmed by the user)

- **Trigger completeness.** Each enrolled repo lists its expected PR-level pipelines. The trigger fires when all of them have finished for the SHA and succeeded; other pipelines are ignored. A missing expected pipeline means "not green", and the trigger polls the Woodpecker API to confirm the set rather than trusting the event stream alone.
- **Affected-test selection** is owned by the repo's Woodpecker PR pipeline, not the control plane. A repo with no PR-level pipeline configured is not enrolled.
- **Strict / up-to-date.** The pipeline updates the PR branch itself when main moves. If the result has the same patch-id as the reviewed diff (no content change), the earlier approval is carried over; otherwise it is re-reviewed. Pending is set on the gate status immediately on every push, so an old green can never merge a new SHA.
- **Finding fingerprint.** Normalised (file path, rule/category, normalised message text), with line numbers excluded. A finding repeated unchanged after a fix attempt counts as a repeat.
- **Round counting.** Only completed review rounds count toward the cap (default 4). CI-red fix pushes count separately, with their own cap of 3, and also escalate.
- **Escalation clear.** The user resumes an escalated PR with a CLI command and a dashboard button that reset the round counters. Nothing else clears it.
- **Reviewer access.** A read-only sandboxed checkout of the PR head, the diff, and the CI logs. The reviewer does not execute project code beyond what CI already ran.
- **Main verification.** One run at a time on the latest main. Merges that land during a run are covered by the next run. Blame is the commit range since the last green main run's SHA, listed in order, not a single guess.

## Left to the builder

Exact table/event schemas, the scaling algorithm, review prompt wording, how the gate credential is stored, UI layout, and the order of delivery. A sensible order is: author registry and GitHub App, then trigger + queue + a single reviewer + gate status + author/fixer notification, then the reviewer pool, then main verification and the dashboard.
