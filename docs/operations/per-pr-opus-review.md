# Per-PR Opus agent review (Woodpecker template)

The required commit status `Opus agent review` is posted for each pull request head as soon as Woodpecker sees the
PR event, instead of waiting for a polling timer. One tiny YAML per repo; the logic lives once on the review host.

```
GitHub PR event -> Woodpecker (label review-host) -> /usr/local/bin/herdr-opus-review -> always-on-review.py review --post
                                                                                     -> status "Opus agent review" on the head SHA
```

The review itself is `deploy/ci-review/always-on-review.py` (Opus read-only in a detached worktree of the PR head, the
script and not the model decides the state). See `deploy/ci-review/README.md` and
`docs/operations/independent-ai-review.md`. Checked against Woodpecker 3.20.1 (`woodpecker-cli lint --strict` is part of
the tests when the CLI is installed).

## (a) The YAML other repos copy

Save as `.woodpecker/opus-review.yaml`, unchanged:

```yaml
when:
  - event: [pull_request, pull_request_metadata]
labels:
  review-host: "true"
concurrency:
  limit: 1
  group: "opus-review"
skip_clone: true
steps:
  - name: opus-review
    image: bash
    commands:
      - /usr/local/bin/herdr-opus-review
```

Why each line:

- `pull_request` fires for opened, synchronized (new push) and reopened. `pull_request_metadata` fires for edits that
  are not a push: a title or label change and, importantly, **a draft marked ready for review**, which does not match
  `event: pull_request`. It is included because a draft head is skipped by the review and nothing else would trigger it
  again. The cost is a short pipeline per metadata edit; the review exits at once when the head already has a terminal
  status.
- `labels: review-host` pins it to the review host's agent, never the hive-heavy pool (that is for the test suites).
- `concurrency` limit 1 queues reviews in the repo. The review script also takes a host-wide lock, so reviews of
  different repos, and the hourly sweep, never run together (they share one Claude login and one model slot).
- `skip_clone: true`: the pipeline itself checks out nothing. The reviewer reads the PR head in its own worktree.
- `image: bash` names an executable on the agent, as in the other `.woodpecker/*.yml` files (local backend, no container).
- The command is an **absolute path** and does not use `$HOME`: the local backend replaces `$HOME` with a throwaway
  directory by default (`WOODPECKER_BACKEND_LOCAL_ISOLATED_HOME=true`). The wrapper looks up the real home itself.
  You may set that option to `false`, but nothing here depends on it.

## (b) One-time operator steps

Nothing here is automated, and no secret value belongs in any file or log.

1. **Install the wrapper and config on the review host** (the host with `gh`, `git`, the `claude` login and pi
   configured; interim host: beast). As the user the agent will run as:

   ```bash
   sudo ln -s <checkout>/scripts/ci/opus-review.sh /usr/local/bin/herdr-opus-review
   mkdir -p ~/.config/herdr-engineering
   cp <checkout>/deploy/ci-review/always-on-review.example.yaml ~/.config/herdr-engineering/always-on-review.yaml
   # edit it: post: true, state_dir, prompt.checklist_file (absolute path), tiers.tier2 (model opus)
   ```

   The wrapper reads the config, the state directory and the `gh` and Claude logins from that user's real home (passwd
   entry), whatever `$HOME` the step has. The symlink runs whatever the checkout contains: keep it on `main` and never
   point it at a checkout that runs PR branches.
2. **Register a Woodpecker agent that takes ONLY labelled work.** `WOODPECKER_AGENT_LABELS=review-host=true` only adds a
   label: the agent would still take unlabelled workflows (structural-gates, artifact-handoff, ...) and run them next
   to the Claude login and the token. Use the required form, a leading `!`:

   ```
   WOODPECKER_AGENT_LABELS=!review-host=true
   WOODPECKER_MAX_WORKFLOWS=1
   WOODPECKER_BACKEND=local
   ```

   With `!review-host=true` the server (`server/scheduler/filter.go`, `requiredLabelsMissing`, in 3.20.1) hands the agent a
   workflow only if it carries `review-host: "true"`; a workflow without that label never reaches it. The agent must
   not carry `cluster=hive-heavy`. Restart it, then check the agent list shows the label.
3. **Create the token file** (no Woodpecker secret on purpose: a secret that does not exist is a compile error that
   fails every pipeline of the repo, and the workflow must stay harmless before the host is ready). As the agent's user:

   ```bash
   install -m 600 /dev/null ~/.config/herdr-engineering/opus-review.token
   $EDITOR ~/.config/herdr-engineering/opus-review.token     # one line: the token
   ```

   The token is a **fine-grained personal access token limited to the enrolled repositories** with Commit statuses
   (read and write) and Pull requests (read and write); Contents read if the repos are private. The wrapper reads it
   into memory only (never an argument, never logged, whitespace stripped) and refuses the file, exit 1, if it is
   missing (the step fails with these instructions, it does not skip), empty, not owned by the user the agent runs as,
   or readable by group or others (it must be mode 0600). The wrapper opens the file once and checks and reads
   through that one descriptor, so it cannot be swapped between the check and the read. A path starting with `-` is
   refused. To use another path, pass `--token-file FILE` in the
   command or set `OPUS_REVIEW_TOKEN_FILE` in the agent's environment. The Claude login and the pi key already live on
   the host.
4. **Set the repo options** for every enrolled repo (see fork safety below): require approval for pull requests (and
   list the trusted authors, see fork safety), and set the pipeline timeout (repo settings) generously. The host-slot
   wait (`--lock-wait`, default 1800 s) applies **per tier**, and `review --tier all` runs two tiers, so the worst case
   is a wait of 1800 s for tier 1, then 900 s of tier 1 (`tiers.tier1.time_cap_s`), a wait of 1800 s for tier 2, then
   3600 s of tier 2 (`tiers.tier2.time_cap_s`): 2 x 1800 + 900 + 3600 = 8100 s = 135 minutes, plus a Codex reviewer's
   cap if one is enabled. Set the timeout to **at least 150 minutes**. A repo already at 720 minutes (the
   herdr-engineering setting) is fine. Lower `--lock-wait` if you lower the timeout; twice the wait plus the tier caps
   must always stay below it.
5. **Enroll a repo**: activate it in Woodpecker (webhook), copy the YAML above into `.woodpecker/opus-review.yaml`, and
   make `Opus agent review` a required status in its ruleset when ready. Nothing else per repo: the review creates its
   own bare clone under `state_dir/clones/` on first use (or set `clones:` in the config).
6. **Keep the timer as a backup**: `cp deploy/ci-review/herdr-always-on-review.{service,timer} ~/.config/systemd/user/`
   (now hourly). It only catches missed webhooks; a head that already has a terminal status is not reviewed twice, and it
   takes the same host-wide slot as the pipeline reviews (one review at a time per host), skipping a PR when the slot is
   busy and trying again an hour later.

### Verify

- Agent label: create a throwaway workflow without `labels:` in a test repo and confirm it is never picked up by this
  agent (the agent log shows no task), while `opus-review` is. Also `woodpecker-cli lint --strict .woodpecker/opus-review.yaml`.
- On the host, as the agent's user, with the token file in place:
  `CI_PIPELINE_EVENT=pull_request CI_REPO=<owner/repo> CI_COMMIT_PULL_REQUEST=<n> /usr/local/bin/herdr-opus-review`
  (with `post: false` in the config it is a dry run: it writes `reviews.jsonl` and posts nothing). Run it with
  `HOME=/nonexistent` too: it must still find the config.
- Open a test PR in an enrolled repo. Within a minute the `opus-review` workflow starts on the review host and the PR
  shows `Opus agent review` as `pending`, then `success` or `failure`, on the head SHA.
- Push another commit: the old status is replaced by a review of the new head.
- Cancel a running pipeline: no `claude` process of the review is left (`pgrep -af 'claude.*--model'`) and
  `state_dir/worktrees/` is empty after the next review starts.
- `~/.local/state/herdr-review/reviews.jsonl` has one line per review, with cost and duration.

## (c) Fork safety: read this before enrolling a repo

Woodpecker 3.20.1 does **not** keep fork pull requests away from the review host. A pull request from a fork runs
**its own copy of the workflow file** on the review host, in the agent's user account, once it is approved to run. That
is arbitrary code from the fork's author, next to the status token file, the `gh` login and the Claude login. The pipeline does not check out the PR, but a PR can
replace `.woodpecker/opus-review.yaml` (or any workflow) with anything. The token is a file on the host, so there is nothing to withhold from such a run.

- **The real control** is the repo setting **require approval for all pull requests** (not just first-time or fork
  contributors), set for each enrolled repo. Nothing runs on the review host until a maintainer approves that PR's
  pipeline.
  With that setting every push and every metadata edit (a pipeline per event) waits for a click, which conflicts with
  automatic review and merging. Set the repo option `approval_allowed_users` to the owner and the bot accounts whose
  PRs may run unattended (Woodpecker checks the PR author, so a push to a trusted author's PR does not need a click).
  PRs by any author not on the list, forks included, still need an approval.
- **Approving a pull request's pipeline approves its code on the review host, with the token.** The token is a file on
  that host, and the approved workflow runs there as the agent's user, so **a fork PR you approve can read the token
  file** (and the `gh` and Claude logins). That is why the token must be a fine-grained PAT limited to commit statuses
  and PR comments on the enrolled repositories only. For a PR from a fork that
  means arbitrary code from outside the organization. Approve a fork PR only after reading its changes to `.woodpecker/`
  and treating it as if you had run it yourself. Prefer not to approve fork PRs on enrolled repos at all.
- Pull requests from branches of the repo itself are trusted to the extent their authors have push access: they can
  also rewrite the workflow. Keep the token's scope to commit statuses and PR comments, and keep this host's `claude`
  and `gh` logins minimal for the same reason.
- The wrapper's check is only a safety net: `gh pr view --jq .isCrossRepository` must print exactly `false`. `true` ends
  the step without a review, anything else (an error, a different answer) fails the step. It cannot stop a malicious
  workflow, which does not call the wrapper.
- Inside a review the reviewer runs read-only tools, but **the token file is not hidden from it**. The claude tier is
  confined to `Read`, `Grep` and `Glob` on the worktree (no shell). The pi tier reads files with no sandbox unless
  `backends.pi.wrapper` provides one, and codex `--sandbox read-only` can still read the whole filesystem. All of them
  run as the agent's user, and the token is a persistent file in that user's home. The diff and PR text are untrusted
  prompt input, so a prompt injection in a diff could make a reviewer read
  `~/.config/herdr-engineering/opus-review.token` and emit it, encoded, in a finding. What reduces this: the
  reviewer's environment is scrubbed (no `GH_TOKEN`, no API keys), the token is registered for redaction at startup
  (plain text only: an encoded copy is not caught), and the token's scope is only commit statuses and PR comments on the
  enrolled repos. Treat it as a low-value, rotatable credential, and give pi a sandbox wrapper that hides `~/.config`.
- **Register the agent with an owner scope.** An agent registered server-wide serves every repo on the Woodpecker
  server, and anyone who can activate a repo there can add `labels: review-host: "true"` and run on this host. Register
  it as a user- or org-scoped agent (or confirm the server is closed to other users).
- **No log masking, no tracing.** Woodpecker masks only its own secrets in logs, and this token is not one. Nothing may
  run the wrapper with `set -x` or `bash -x`, and the wrapper itself never prints the token.
- The wrapper uses the token only from the token file; an ambient `GH_TOKEN` or `GITHUB_TOKEN` on the agent is unset
  and never used.

## Behavior to know

- **Exit 4 and 3 become exit 0.** The review exits 4 when it did nothing and that is expected: the head already has a
  terminal status, another run holds the PR, or the host's review slot is taken (with `--host-lock-wait 0`). It also
  exits 4 for a draft PR, a failure cooldown, an exhausted daily budget, a disabled tier or, with
  `gates.wait_for_green`, gates that are not green. The wrapper cannot tell these apart, so a green pipeline does **not**
  prove a review happened: the status on the head is the truth. The hourly sweep retries what can be retried. Exit 3 means
  the head moved; the push that moved it starts its own review. Exit 1 (failed or a lock wait that ran out) and 2 (refused)
  stay red and leave the status `pending`.
- **Tiers.** `review` runs tier 1 (advisory) and tier 2 (the required status) independently: a failing tier 1 is
  reported in the exit code but never stops Opus.
- **Cancelled pipelines.** Woodpecker cancels by killing the step's process group, and the reviewer runs in its own
  session. A small watchdog process kills the reviewer's whole group when the review process disappears, however it was
  stopped, and the next review (holding the host slot) removes leftover worktrees and `refs/herdr-review/*` in every
  clone. The pipeline timeout must be at least the sum in step 4, otherwise Woodpecker cancels a review that was only slow.
- **Shared clones.** The review's refs are named by PR number (`refs/herdr-review/pr-N`), so two repos must never share
  one clone directory; the managed clones are one per repo. Managed clone names are case-sensitive on disk
  (`Owner__Name.git`); use one spelling of the repo name in enrollment.
- **Draft PRs** are skipped until marked ready, which arrives as `pull_request_metadata` (included in the YAML above).

- **Environment the wrapper sets.** It overrides HOME with the real home from passwd and unsets the `XDG_*` variables,
  so a custom `XDG_CONFIG_HOME` is ignored: put the config at `~/.config/herdr-engineering/always-on-review.yaml`, or
  pass `--config FILE`.
- **Exit 4 while a sweep holds the PR lock.** If the hourly sweep (or another run) is reviewing the same PR, the
  pipeline's review exits 4 (the PR lock is never waited for, only the host slot) and the wrapper turns that into exit
  0. The sweep reviews the head it pinned when it started. If the pipeline was for a newer head, the sweep ends with the
  head moved and nothing posted for the new head, which then waits for the next hourly sweep (up to an hour). If the
  pipeline was for the same head, the sweep's review covers it; if that fails, the next sweep retries.
- **Cleanup.** The first review that will actually run in an invocation removes leftover worktrees and
  `refs/herdr-review/*`; it runs `git worktree prune` only in the managed clones, never in your own `clone:`, and a
  no-op run (draft, already reviewed) cleans nothing. Fetches into your own `clone:` also reset inherited credential
  helpers, so a global `store` helper cannot save the token there.

## When it does not post

- Pipeline red with `config not readable`, `review script not found` or `cannot determine the home directory`: step 1 is
  incomplete. Exit 127 means `/usr/local/bin/herdr-opus-review` is missing.
- Status stays `pending`: the review could not finish (time cap, bad answer, Claude not logged in). See the pipeline log
  and `reviews.jsonl`; the hourly sweep retries it once the pending status is stale, and the failure cooldown applies.
- No pipeline at all: the agent label or the webhook (Woodpecker server log) is the problem; the timer sweep reviews the
  head within the hour.
