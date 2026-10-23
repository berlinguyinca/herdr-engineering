# Review gate (operator guide)

The review gate is a control-plane loop that watches open pull requests on the
GitHub repos you enroll. For each PR head it waits for green Woodpecker CI,
queues an AI review, posts the commit status `herdr/review-gate`, and merges
through a GitHub App once the review has passed and CI is green. Design:
`docs/superpowers/specs/2026-10-22-automated-review-merge-gate-design.md`.

> **This release ships the gate core only.** Reviewer workers and author
> notification are not part of it, so nothing approves PRs yet. An enabled gate
> will only ever post `pending` or `failure` statuses until the reviewer plan
> lands.

## 1. Fleet config

The gate is configured in the `control_plane` section of the central fleet
config. It needs durable storage: the web server refuses to start with an
enabled gate when `control_plane.pg_dsn` is not set.

```yaml
sections:
  control_plane:
    values:
      pg_dsn: postgresql://...            # required while review_gate.enabled
      review_gate:
        enabled: true
        poll_seconds: 15                  # >= 5, default 15
        app:
          app_id: 123456
          installation_id: 78901234
          private_key_file: /etc/herdr/review-gate-app.pem   # absolute path, regular file, mode 0600
        repos:
          - repo: github.com/acme/widgets             # canonical github.com/<owner>/<name>
            required_events: [pull_request]           # Woodpecker events that must all be green (default [pull_request])
            review_cap: 4                             # FAIL verdicts before escalation (default 4)
            ci_fix_cap: 3                             # red CI rounds before escalation (default 3)
```

An enabled section with invalid values fails loudly at server start. A missing
or disabled section turns the gate off, and its API routes return 404.
`required_checks` is accepted and validated, but the gate does not use it in
this release. CI identity comes from the Woodpecker event types in
`required_events`, and every one of them must have a successful run for the
head SHA.

## 2. GitHub App

Create a GitHub App and install it on every enrolled repo. Repository
permissions:

| Permission      | Access         | Used for                       |
|-----------------|----------------|--------------------------------|
| Commit statuses | Read & write   | `herdr/review-gate` status     |
| Pull requests   | Read & write   | listing PRs, merge             |
| Contents        | Read & write   | merge and update-branch        |
| Checks          | None           | (not used)                     |

The private key stays on the control-plane host. Installation tokens are
minted on demand and live only in memory.

## 3. Branch protection

On each enrolled repo's protected branch:

- require the status check `herdr/review-gate`;
- require branches to be up to date before merging (strict checks).

Keep personal access tokens out of the loop. Agents should get tokens that can
push branches and open PRs but cannot set commit statuses or merge. Only the
App posts `herdr/review-gate` and merges.

## 4. Woodpecker prerequisite

The gate reads CI from the control plane's Woodpecker monitor. The `ci` section
of the central fleet config (Woodpecker `url` and `token`) must be set, and the
token's user must be able to see every enrolled repo. A repo the monitor cannot
see stays `pending` for ever.

## 5. Operating it

Read endpoints (any API key role):

- `GET /api/v1/review-gate/prs`: state, head SHA, round counts, reason and
  last findings for every PR the gate has seen.
- `GET /api/v1/review-gate/queue`: queue depth, jobs and the last tick summary.

**Escalation.** A PR escalates when it hits `ci_fix_cap` red CI rounds or
`review_cap` failed reviews, or when a review repeats an earlier finding. It
then holds a `failure` status until an operator resumes it:

```
POST /api/v1/review-gate/prs/github.com/<owner>/<name>/<number>/resume
X-API-Key: <operator key>
Content-Type: application/json

{}
```

Resume resets the round counters and sends the PR back to waiting for CI. A
non-escalated PR answers 409, an unknown PR 404, and a failed durable write 503
(nothing changes). The status "cannot queue review; push a commit" means the
current head cannot be queued again; push a new commit.

## 6. Known limits

- **One gate per fleet.** Run the gate on one control-plane server only;
  nothing prevents two instances from fighting.
- **Config is read at startup.** Restart the server to change enrollment, caps
  or the App.
- **Every new head is re-reviewed.** Approval does not carry over a rebase or
  an identical re-push yet.
- **No reviewers or notifications yet** (see the note at the top).
- **Scale.** Queue and PR state are refolded from the event log every tick,
  which is comfortable for roughly tens of open PRs.
- **CI history window.** The Woodpecker monitor keeps recent runs only. An
  approved PR whose CI runs scroll out of that window waits (`pending`) instead
  of merging.
- In-memory memos (posted statuses, merge back-off) reset on restart, so a
  restart re-posts each status once.
