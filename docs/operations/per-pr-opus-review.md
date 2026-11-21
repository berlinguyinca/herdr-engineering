# Per-PR Opus agent review (Woodpecker template)

The required commit status `Opus agent review` is posted for each pull request head as soon as Woodpecker sees the
PR event, instead of waiting for a polling timer. One tiny YAML per repo; the logic lives once on the review host.

```
GitHub PR event -> Woodpecker (label review-host) -> herdr-opus-review -> always-on-review.py review --post
                                                                      -> status "Opus agent review" on the head SHA
```

The review itself is `deploy/ci-review/always-on-review.py` (Opus read-only in a detached worktree of the PR head, the
script and not the model decides the state). See `deploy/ci-review/README.md` and
`docs/operations/independent-ai-review.md`.

## (a) The YAML other repos copy

Save as `.woodpecker/opus-review.yaml`, unchanged:

```yaml
when:
  - event: pull_request
labels:
  review-host: "true"
concurrency:
  limit: 1
  group: "opus-review"
skip_clone: true
steps:
  - name: opus-review
    image: bash
    environment:
      OPUS_REVIEW_GITHUB_TOKEN:
        from_secret: opus_review_github_token
    commands:
      - '"$HOME/.local/bin/herdr-opus-review"'
```

Why each line:

- `pull_request` fires for opened, synchronized (new push) and reopened. A new push moves the head, so the status for
  the old head goes stale and the new event reviews the new head.
- `labels: review-host` pins it to the review host's agent, never the hive-heavy pool (that is for the test suites).
- `concurrency` limit 1 queues reviews in the repo. The wrapper also takes a host-wide lock, so reviews of different
  repos never run together either (they share one Claude login and one slot).
- `skip_clone: true`: nothing from the PR is checked out or run by the pipeline. The reviewer reads the PR head in its own
  read-only worktree (`Read`, `Grep`, `Glob` only, no shell).
- `image: bash` names an executable on the agent, as in the other `.woodpecker/*.yml` files (local backend, no container).

## (b) One-time operator steps

Nothing here is automated, and no secret value belongs in any file or log.

1. **Install the wrapper and config on the review host** (the host with `gh`, `git`, `flock`, the `claude` login and
   pi configured; interim host: beast):

   ```bash
   ln -s <checkout>/scripts/ci/opus-review.sh ~/.local/bin/herdr-opus-review
   mkdir -p ~/.config/herdr-engineering
   cp <checkout>/deploy/ci-review/always-on-review.example.yaml ~/.config/herdr-engineering/always-on-review.yaml
   # edit it: post: true, state_dir, prompt.checklist_file (absolute path), tiers.tier2 (model opus)
   ```

   The agent runs as the user that owns `~/.local/bin` and the Claude login. Keep the checkout current (`git pull`):
   the symlink runs whatever it contains. Do not point it at a checkout that runs PR branches.
2. **Register a Woodpecker agent on that host with the label `review-host=true`** (agent config
   `WOODPECKER_AGENT_LABELS=review-host=true`; use `WOODPECKER_MAX_WORKFLOWS=1`; backend `local`). It must not
   carry `cluster=hive-heavy`. Restart the agent and check that it shows up in the server's agent list with the label.
3. **Add the secret `opus_review_github_token`** in Woodpecker, at user or global scope so every enrolled repo
   inherits it, limited to the `pull_request` event. Its value is a GitHub token that can write commit statuses and PR
   comments on the enrolled repos (fine-grained: Commit statuses RW, Pull requests RW, Contents R; or a classic token
   with `repo`). It is the only secret this pipeline uses: the Claude login and the pi key already live on the host.
   Do not give the secret to `push`, `manual` or `cron` events.
4. **Enroll a repo**: activate it in Woodpecker (webhook), copy the YAML above into `.woodpecker/opus-review.yaml`,
   and make `Opus agent review` a required status in its ruleset when ready. Nothing else per repo: the review
   creates its own bare clone under `state_dir/clones/` on first use (or set `clones:` in the config).
5. **Keep the timer as a backup**: `cp deploy/ci-review/herdr-always-on-review.{service,timer} ~/.config/systemd/user/`
   (now hourly). It only catches missed webhooks; a head that already has a terminal status is not reviewed twice.

### Verify

- On the host, with a throwaway token in the environment of the command, not on any command line:
  `CI_PIPELINE_EVENT=pull_request CI_REPO=<owner/repo> CI_COMMIT_PULL_REQUEST=<n> OPUS_REVIEW_GITHUB_TOKEN=... herdr-opus-review`
  (with `post: false` in the config it is a dry run: it writes `reviews.jsonl` and posts nothing).
- Open a draft-free test PR in an enrolled repo. Within a minute the `opus-review` workflow starts on the review host
  and the PR shows `Opus agent review` as `pending`, then `success` or `failure`, on the head SHA.
- Push another commit: the old status is replaced by a review of the new head.
- A PR from a fork must show the workflow ending in "no opus_review_github_token was injected" or "comes from a fork",
  exit 0, and no status.
- `~/.local/state/herdr-review/reviews.jsonl` has one line per review, with cost and duration.

## (c) Fork safety

- The token is a write credential, so the review must never see attacker-controlled code with it, and no PR code runs
  in the pipeline. The workflow does not check out anything; the reviewer runs read-only tools in a worktree and its
  environment is scrubbed (no `GH_TOKEN`, no API keys). The diff and PR text are untrusted prompt input.
- Fork PRs are skipped in three independent ways: the secret is limited to the `pull_request` event and Woodpecker
  injects no secret into a fork's pipeline (so the wrapper sees no token and exits 0); the wrapper asks GitHub whether
  the head is in a fork (`isCrossRepository`) and exits 0 if so; and a question it cannot answer is an error, never
  a review. In Woodpecker, leave the repo's "trusted" and fork-approval settings at their defaults.
- Do not widen the secret to other events or to `workflow` files you do not control, and do not run this step from
  `pull_request_closed` or `push`. A PR can edit `.woodpecker/opus-review.yaml` itself: Woodpecker runs the
  pull request's version of the file, so require approval for workflow changes from non-members, and keep the host's
  `claude` and `gh` logins minimal for that reason.
- The wrapper takes the token only from `opus_review_github_token`; an ambient `GH_TOKEN` or `GITHUB_TOKEN` on the agent is
  unset and never used.

## When it does not post

- Pipeline red with `config not readable` or `review script not found`: step 1 is incomplete.
- Status stays `pending`: the review could not finish (time cap, bad answer, Claude not logged in). See the pipeline log
  and `reviews.jsonl`; the hourly sweep retries it once the pending status is stale, and the failure cooldown applies.
- No pipeline at all: the agent label or the webhook (Woodpecker server log) is the problem; the timer sweep reviews
  the head within the hour.
