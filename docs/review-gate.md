# Review gate (operator guide)

The review gate is a control-plane loop that watches open pull requests on the
GitHub repos you enroll. For each PR head it waits for green Woodpecker CI,
queues an AI review, posts the commit status `herdr/review-gate`, and merges
through a GitHub App once the review has passed and CI is green. Design:
`docs/superpowers/specs/2026-10-22-automated-review-merge-gate-design.md`.

The loop, end to end:

1. A push gives the PR a new head SHA. The gate posts `pending` and waits for green CI on that SHA.
2. CI goes green, and the gate queues one review job for that SHA.
3. A reviewer worker claims the job, sends the bounded diff to its model and posts the model's text back.
4. On PASS the gate posts `success` and merges at exactly the reviewed SHA. On FAIL it posts `failure` and sends the findings to the author.
5. The author pushes a fix, which is a new head, so the cycle starts again. Repeated or capped failures escalate to an operator.

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
        reviewer:                                     # optional; every key optional, integers >= 1
          lease_seconds: 900                          # how long a claimed job stays with one worker (default 900; 30..86400)
          max_diff_bytes: 400000                      # diff bytes sent to the reviewer (default 400000; 1000..5000000)
          max_findings: 50                            # findings a FAIL may carry (default 50)
          max_attempts: 3                             # claims per job before escalation (default 3)
          max_output_bytes: 200000                    # reviewer output bytes accepted (default 200000; 1000..2000000)
        notify:                                       # optional; both commands optional
          fixer_command: [/usr/local/bin/start-fixer, "--repo={repo}", "{number}", "{branch}", "{sha}"]
          notify_command: [/usr/local/bin/page-operator, "{repo}#{number}"]
```

An enabled section with invalid values fails loudly at server start. A missing
or disabled section turns the gate off. Its read and resume routes then return
404, and the reviewer worker routes return 503 (`review gate disabled`).
`required_checks` is accepted and validated, but the gate does not use it in
this release. CI identity comes from the Woodpecker event types in
`required_events`, and every one of them must have a successful run for the
head SHA.

The `notify` commands are described in section 7. Neither the config nor its
commands carry secrets. A command that needs a credential reads it from its
own file.

## 2. GitHub App

Create a GitHub App and install it on every enrolled repo. Repository
permissions:

| Permission      | Access         | Used for                       |
|-----------------|----------------|--------------------------------|
| Commit statuses | Read & write   | `herdr/review-gate` status     |
| Pull requests   | Read & write   | listing PRs, review diff, merge |
| Contents        | Read & write   | merge and update-branch        |
| Checks          | None           | (not used)                     |

The private key stays on the control-plane host. Installation tokens are
minted on demand and live only in memory. Reviewer workers never see a GitHub
credential.

## 3. Branch protection

On each enrolled repo's protected branch:

- require the status check `herdr/review-gate`;
- require branches to be up to date before merging (strict checks).

Keep personal access tokens out of the loop. Agents should get tokens that can
push branches and open PRs but cannot set commit statuses or merge. Only the
App posts `herdr/review-gate` and merges.

**Pin the required check to the App.** Configure the required
`herdr/review-gate` check so that only the GitHub App may provide it: in
branch protection or a ruleset, set the required check's expected source to
the App. Verify that this setting exists in your GitHub plan. Without it, any
token that can write commit statuses (including a classic personal token with
`repo` scope) can set `herdr/review-gate` to `success` itself.

**CI is not a trust anchor.** Woodpecker `pull_request` pipelines take their
configuration from the PR, so the PR author can influence what "CI green"
means. The gate requires green CI, but the review is the real check.

## 4. Woodpecker prerequisite

The gate reads CI from the control plane's Woodpecker monitor. The `ci` section
of the central fleet config (Woodpecker `url` and `token`) must be set, and the
token's user must be able to see every enrolled repo. A repo the monitor cannot
see stays `pending` for ever.

## 5. Reviewer workers

### The `reviewer` API key role

Workers authenticate with a control-plane API key whose role is `reviewer`.
Add it to `control_plane.api_keys` (key -> role) like any other key. The key
is sent in the `X-API-Key` header, not `Authorization`.

A `reviewer` key can read like a viewer, and it can POST to exactly four
routes:

- `POST /api/v1/review-gate/claim`
- `POST /api/v1/review-gate/jobs/<job_id>/heartbeat`
- `POST /api/v1/review-gate/jobs/<job_id>/result`
- `POST /api/v1/review-gate/jobs/<job_id>/failure`

No other role can use these routes, operator included. A server running open
(no API keys) therefore cannot serve reviewers. A `reviewer` key cannot ingest,
resume a PR or change anything else. It can read everything a viewer can,
which includes the dashboard data.

Keep the key on reviewer hosts only. Whoever holds it can claim jobs and submit
any verdict, PASS included, for a job they claimed.

**Claim tokens.** Each claim returns a `claim_token`. The heartbeat, result and
failure calls must send it, and the control plane checks it before anything
else. Worker ids are not secret, and job ids are visible on `/queue` (who holds
a claim is shown to operators only), so the token is what protects a claim:
another `reviewer` key holder who knows the job and worker id still cannot
heartbeat, submit or fail it. A later claim of the same job invalidates older
tokens. Tokens live in the control plane's memory, so a control-plane restart
invalidates every in-flight token: those reviews are answered `stale` and
wasted, and the jobs are offered again when their leases expire.

### Worker config

Each worker reads one YAML file. It takes no environment variables, and it
holds no secrets inline: keys are read from files, by path.

```yaml
control_plane_url: https://cp.example.org      # https, or http only for loopback (127.0.0.1, ::1, localhost)
api_key_file: /etc/herdr/reviewer.key          # the reviewer-role key; absolute path, mode 0600
worker_id: bender-review-1                     # [A-Za-z0-9._:/-], 1-128 characters
poll_seconds: 10                               # idle wait between claims (default 10)
model:
  base_url: https://llm.example.org            # OpenAI-compatible; same http/https rule
  model: qwen3-review                          # sent as reviewer_model when claiming
  api_key_file: /etc/herdr/review-model.key    # optional; absolute path, mode 0600
  max_tokens: 4096                             # default 4096
  timeout_seconds: 300                         # default 300
```

Key files must be absolute paths to regular files with mode 0600. They are
re-read on every request, so rotating a key needs no restart. A key file that
goes bad stops the worker. URLs with user info, a query or a fragment are
refused.

Run it:

```
herdr-eng review-gate worker --config /etc/herdr/review-worker.yaml          # loop until SIGINT/SIGTERM
herdr-eng review-gate worker --config /etc/herdr/review-worker.yaml --once   # one claim; prints handled or idle
```

Exit codes: 0 for a clean stop, 1 when the control plane refuses the key
(401/403), and 2 for a config error.

**The reviewer model must differ from the author's model.** When the gate
knows the agent that wrote a PR, it records that agent's model on the job. A
worker whose `model` equals it (exact string) never gets that PR's job. If
every worker runs the author's model, the job stays queued.

### Failures and back-off

A model error, a malformed reply or an unusable verdict is reported to the
control plane, and the job goes straight back to the queue. Each claim spends
one of the job's `max_attempts`. So that a worker with a dead model does not
burn every attempt in milliseconds, it waits `poll_seconds` after each job that
did not end in a recorded or stale result, doubling per consecutive failure up
to 300 s. A recorded result or an idle claim resets the wait.

Attempts are spent per claim, also when the cause is not the reviewer (GitHub
unreachable, a worker killed mid-review). An outage longer than about
`max_attempts x lease_seconds` (about 45 minutes with the defaults) therefore
escalates every PR it touched, and an operator must `resume` them (section 8).

A model call has a total wall-clock deadline of `model.timeout_seconds`. A
model endpoint that answers slowly, byte by byte, is abandoned at the deadline
and reported as `model_timeout`, so it cannot hold a lease through
heartbeats.

A stopped worker abandons its job without posting. The job is offered again
when its lease expires, but the abandoned claim has spent one of its
`max_attempts`. Restarting workers mid-review again and again can therefore
escalate a PR.

### Reviewer pool

`review-gate pool` runs several workers in one process and sizes them from the
review queue, so a burst of PRs is reviewed in parallel and the extra workers
go away afterwards. Each slot is a thread running the same worker loop under
its own id, `<worker_id>-<n>`. The pool config is the worker config above plus
a `pool` mapping; `worker_id` becomes the slot-id prefix.

```yaml
control_plane_url: https://cp.example.org
api_key_file: /etc/herdr/reviewer.key
worker_id: bender-review                       # slots: bender-review-1, bender-review-2, ...
model:
  base_url: https://llm.example.org
  model: qwen3-review
pool:
  min_workers: 1                               # 0-64, default 1 (0: no slot while the queue is empty)
  max_workers: 4                               # 1-64, default 4, at least min_workers
  idle_seconds: 120                            # a slot idle this long may be retired (10-86400, default 120)
  interval_seconds: 10                         # how often the queue is read (1-3600, default 10)
```

```
herdr-eng review-gate pool --config /etc/herdr/review-pool.yaml   # loop until SIGINT/SIGTERM
```

**Scaling rule.** Every `interval_seconds` the pool reads
`GET /api/v1/review-gate/queue` (the `reviewer` key may) and counts the queued
jobs its model may claim: jobs whose author model differs from the pool's
`model`, as for a single worker. Idle slots already claim queued jobs, so a
non-empty count means every slot is busy or backing off. The pool then adds
`min(count, max_workers - slots)` slots. With nothing eligible it retires one
slot per evaluation, and only a slot idle longer than `idle_seconds`, down to
`min_workers`. A slot is retired only between claim cycles, so scaling never
abandons a claim. A slot in failure back-off counts as busy and can trigger a
scale-up, bounded by `max_workers`.

**One pool per host and model.** Pool size is per process: two pool processes
with `max_workers: 4` can run eight workers. Give each process its own
`worker_id` prefix. Pools are threads, not herdr sessions: a crash of the
process drops all its slots, and their jobs are offered again when the leases
expire.

**Failures.** If the queue cannot be read (unreachable, an error status, a
malformed or over-1-MiB body) the pool keeps its size and waits
`interval_seconds`, doubling per consecutive failure up to 300 s. A 401/403
from the queue or from any slot, or a key file that goes bad, stops the whole
pool; the exit codes are those of `worker` (1 refused key, 2 config error) plus 3 for an unexpected internal error, which systemd
restarts. With `min_workers: 0` a pool whose queue reads keep failing (an old control plane,
a proxy error) has no slots and reviews nothing; it logs a warning per failed read, so watch
for those. The pool never reads or logs key contents; log lines carry counts and class names.

A systemd unit needs no `Environment=` lines:

```ini
[Unit]
Description=herdr review gate reviewer pool
After=network-online.target

[Service]
ExecStart=/usr/local/bin/herdr-eng review-gate pool --config /etc/herdr/review-pool.yaml
Restart=on-failure
RestartPreventExitStatus=1 2
User=herdr-review

[Install]
WantedBy=multi-user.target
```

### What gets reviewed

- The reviewer sees only the PR title, body and diff, bounded to
  `max_diff_bytes` (400 KB by default), plus the previous round's findings.
  It has no repo checkout and no tools.
- A diff over the bound is truncated and marked as such. A PASS on a truncated
  diff is refused (`diff too large to review fully`), so a huge PR escalates
  instead of merging unread.
- An approval is for the base branch the reviewer saw. If the PR is retargeted
  to another base at the same head, the gate does not merge. The status says
  "base branch changed, re-reviewing" and the head is reviewed again against
  the new base. Without this, a PR reviewed against branch X, which holds
  unreviewed commits, could be retargeted to main and merge those commits. An
  approval with no recorded base is re-reviewed the same way. That covers one
  from an older release, or one recorded across a control-plane restart.
  A change of the base branch name or base commit while the diff is fetched
  voids that fetch, so no verdict is recorded for it. That includes a plain
  forward move of the base branch; the job is then retried after its lease
  expires. Right before merging, the gate re-reads the PR:
  - a retarget since the last poll leads to a re-review;
  - a moved head skips the merge;
  - a closed or draft PR, or a failed read, skips it and backs off.

  What remains open is a short window of two GitHub round trips after that
  read: the `success` status post, then the merge call.
- The PR text is untrusted. Only one strict JSON object counts as a verdict
  (`{"verdict": "PASS" | "FAIL", "findings": [...]}`). Prose, a verdict
  embedded in other text, or anything else is invalid output and spends an
  attempt. This stops stray "PASS" text from approving. It does not stop a
  model that is persuaded to emit a PASS object, so branch protection, CI and
  the same-model exclusion still matter.

## 6. Operating it

Read endpoints (any API key role):

- `GET /api/v1/review-gate/prs`: state, head SHA, round counts, reason and
  last findings for every PR the gate has seen.
- `GET /api/v1/review-gate/queue`: queue depth, jobs and the last tick summary.
- `GET /api/v1/review-gate/notices`: the newest 100 notifications, newest
  first, each with `key`, `sha`, `kind` (`changes_requested`, `ci_red`,
  `escalated`), `channel` (`author`, `fixer`, `user`), `ok` and `at` (epoch
  seconds). Details such as command output are never shown.

**Escalation.** A PR escalates (the `reason` in `/prs`) when:

- it hits `ci_fix_cap` red CI rounds (`ci fix cap reached`);
- it hits `review_cap` failed reviews (`review cap reached`);
- a review repeats an earlier finding (`repeated finding`);
- the reviewer kept producing unusable output (`invalid review output`);
- workers kept reporting failure (`reviewer failed repeatedly`);
- `max_attempts` claims ended with no verdict (`review could not be completed`);
- the diff was too large to pass (`diff too large to review fully`).

An escalated PR holds a `failure` status until an operator resumes it.

```
POST /api/v1/review-gate/prs/github.com/<owner>/<name>/<number>/resume
X-API-Key: <operator key>
Content-Type: application/json

{}
```

Resume resets the round counters and sends the PR back to waiting for CI. The
same head is then reviewed again. A non-escalated PR answers 409, an unknown PR
404, and a failed durable write 503 (nothing changes). The status "cannot
queue review; push a commit" means the current head cannot be queued again;
push a new commit.

## 7. Notifications

When a PR needs work (`changes_requested` or `ci_red`), the gate tries, in
order, until one delivers:

1. **Author.** The agent that wrote the PR, when it has a live control-plane
   session, gets a message with the findings or failed CI runs. The findings
   are marked as untrusted text.
2. **Fixer.** Otherwise `notify.fixer_command` runs, if configured.
3. **User notice.** Otherwise a notice is recorded for `/notices`.

Each channel is tried at most once per (head SHA, kind), also across restarts.

An **escalation** never goes to the author. It runs `notify.notify_command`, if
configured, and always records a user notice. A push to an escalated PR is a
new head, so it pages again. Only `resume` clears the escalation.

**Resume starts a new notification round.** Notifications are deduplicated
per head SHA, kind and resume generation, which is the number of resumes the
PR has had. If a resumed PR escalates again, or gets changes requested again,
on the same head, the operator is paged again and the author gets a new
message. Within one generation each channel is still tried only once.

### Command contract (`fixer_command`, `notify_command`)

- An argv list, never a shell string. The first element is an absolute path to
  the executable and contains no placeholder.
- Placeholders `{repo}`, `{number}`, `{branch}` and `{sha}` are allowed in the
  later elements only, with no format spec. Values are validated before
  substitution; a bad value means the command is not launched.
- The message (the same text an author would get, at most 8000 characters) is
  written to stdin.
- The command runs in `/`, in its own session, with an environment of exactly
  `PATH=/usr/bin:/bin`.
- It has 60 s. On timeout its whole process group is killed and the attempt
  counts as failed. A command that starts long work must detach and close
  stdout and stderr, or it hits the timeout.
- Exit 0 means delivered. Anything else means failed, and the next channel
  runs.
- At most **one** external command is launched per gate tick. Launches over
  that budget are deferred to later ticks, so a backlog drains at one command
  per `poll_seconds`.

## 8. Known limits

- **One gate per fleet.** Run the gate on one control-plane server only;
  nothing prevents two instances from fighting.
- **Config is read at startup.** Restart the server to change enrollment, caps,
  reviewer or notify settings, or the App.
- **Every new head SHA is re-reviewed.** Approval does not carry over to a new
  SHA, even a rebase or amend with an identical diff.
- **Reviewer pool size is whatever you start.** Nothing scales workers with
  queue depth yet.
- **Same-model exclusion uses the original author's model.** A fixer started
  by `fixer_command` may run a different model than the pinned author. The
  exclusion still compares against the original author.
- **Author lookup is a heuristic.** It matches a session by branch name. A
  session in another repo on the same branch name can match when that session
  recorded no working directory.
- **Reviewer model after a restart.** A verdict records `reviewer_model`
  `unknown` when the control plane restarted between the claim and the
  submit. Nothing decides on that field.
- **Worker 429/413 handling.** A worker waits out a 429 on claim
  (`Retry-After`, at most 300 s). A 413 on a result posts the failure
  `output_too_large`, and any other non-200 on a result (429 included) posts
  `result_error`; both spend an attempt. A 429 on a heartbeat does not count
  as a lost lease.
- **Files without a patch count as truncated.** GitHub returns some files with
  no `patch` (binary files, pure renames, very large single-file diffs), and so
  does a PR with 300 or more files. Such a diff counts as truncated, so the PR
  can never PASS and escalates with `diff too large to review fully`. Split the
  PR or merge it by hand, then `resume` or close it.
- **The diff is read from the PR's live file list.** The reviewer's diff is
  fetched by PR, not by commit, and the head is checked before and after the
  fetch. An author who force-pushes other content and back within that 1 to
  3 s window could get a PASS for content the reviewer did not see. A
  compare-by-SHA fetch, with the diff bound to the base and head commits, is
  the follow-up that would close this window completely.
- **Scale.** Queue and PR state are refolded from the event log every tick,
  which is comfortable for roughly tens of open PRs.
- **CI history window.** The Woodpecker monitor keeps recent runs only (the
  latest 20 per repo). An approved PR whose CI runs scroll out of that window waits
  (`pending`) instead of merging.
- In-memory memos (posted statuses, merge back-off) reset on restart, so a
  restart re-posts each status once.
