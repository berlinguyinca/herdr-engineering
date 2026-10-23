# Independent AI review

The ruleset on `berlinguyinca/herdr-engineering` requires a commit status named exactly `Independent AI review`.
`deploy/ci-review/ai-review.py` posts it. The script asks a reviewer from a different model family than the
author (today: codex reviews Claude's PRs) to review the diff inside the collab sandbox. The script, not the
model, then decides the state. Design details and the sandbox's gaps are in `deploy/ci-review/README.md`.

## Running it

```bash
cp deploy/ci-review/ai-review.example.yaml ~/.config/herdr-engineering/ai-review.yaml   # edit repos:, label:
python3 deploy/ci-review/ai-review.py review --repo berlinguyinca/herdr-engineering --pr 160 \
    --config ~/.config/herdr-engineering/ai-review.yaml --dry-run      # read + review, post nothing
python3 deploy/ci-review/ai-review.py review --repo berlinguyinca/herdr-engineering --pr 160 \
    --config ~/.config/herdr-engineering/ai-review.yaml                # post the status (and comment)
python3 deploy/ci-review/ai-review.py poll --config ~/.config/herdr-engineering/ai-review.yaml
```

Requirements:

- `gh`, signed in as an account that can post statuses and comments on the repo;
- `codex`, signed in (`~/.codex/auth.json`);
- bubblewrap;
- PyYAML.

For unattended runs, install the example user units `deploy/ci-review/herdr-ai-review.{service,timer}`
(`systemctl --user enable --now herdr-ai-review.timer`, and `loginctl enable-linger`). `poll` is idempotent: a
head that already has a terminal status is not reviewed again. Re-run a head by hand with `review`; it posts a
new status and updates the existing comment.

Statuses:

- `pending`: "Review running (codex)".
- `success`: "Approved by codex: code+security review, 0 blocking (visual review not run)".
- `failure`: "Blocked: N findings — see PR comment". The comment, marked `<!-- independent-ai-review -->`, lists
  each finding with file:line and why.

If the review itself could not run, nothing terminal is posted and the pending status stays. Examples are the
reviewer missing or failing, a malformed verdict, a sandbox or proxy error, or a timeout. `poll` retries that
head once the pending status is older than `poll.pending_stale_minutes` (120). A head that moves mid-review gets
nothing; its replacement is reviewed on the next poll.

## What it checks

- Correctness, security and tests, as the reviewer reports them in `checked`. The status names only the aspects
  that every chunk reported.
- Every blocking finding must cite a file in the PR. A finding on a PR file whose line is not shown in the diff
  still blocks, marked "line unverified". A finding citing a file not in the PR is dropped, and the status says
  "N finding(s) dropped (cited code not in the PR)".
- Independence: the reviewer's family must differ from every model family in the commits' `Co-Authored-By`
  trailers (default author family: claude).

## What it does NOT check

- **No visual review.** There is no screenshot or UI step. The status always says "(visual review not run)", and
  `visual_review: true` is refused. UI changes still need a human or a separate visual check.
- **Skipped files.** Binary, lock and generated files (`skip_globs`) are not reviewed and are listed under "Not
  checked".
- **No tests are run.** That is CI's job. The reviewer can read the checkout, but nothing is executed for it.
- **Not a human review.** It is no substitute for one, and it does not approve the PR in GitHub's review sense.

## Limits: advisory strength

- **Prompt injection.** The diff, the PR title and body, and the checkout are untrusted. Text in them can try to
  make the model approve. The mitigations:
  - the content goes on stdin between random fences, labelled as data;
  - the reviewer has no GitHub access and no network beyond its model endpoint;
  - its output must be a strict schema, or the run fails;
  - the poster decides the state, drops findings on files outside the PR (and says so), and lets the model narrow its claims but never widen
    them;
  - one failed chunk fails the whole run.

  None of this stops a successful injection from producing "0 blocking". Treat `success` as "an independent
  model found nothing", not as proof.
- **Exfiltration.** The box can reach the model endpoint, and holds a copy of the codex login. Injected text could
  send the diff, which is in the PR anyway, or the login token out through the allowed hosts. See the README's
  "what is NOT isolated".
- **First real run is unverified.** It is not yet confirmed that codex works through the egress proxy, that the
  default allow list is complete, or that its nested sandbox works. Run it once with `keep_workdir: true`, and
  check the run's `blocked.jsonl`.
- **Big PRs cost several reviewer runs.** PR #160 (550 KB, 33 files) is 12 chunks at 60 KB. Each chunk sees only
  its own files, plus the read-only checkout for context, so a cross-file bug can be missed.
