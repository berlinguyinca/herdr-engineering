# Independent AI review

The ruleset on `berlinguyinca/herdr-engineering` requires a commit status named exactly `Independent AI review`.
`deploy/ci-review/ai-review.py` posts it. The script sends the PR's diff to a model from a different family than
the author, chosen from the models `llm.metabolomics.us` advertises. Today that means a deepseek or qwen model
reviews Claude's PRs. The script, not the model, then decides the state. Details are in
`deploy/ci-review/README.md`.

## Running it

```bash
cp deploy/ci-review/ai-review.example.yaml ~/.config/herdr-engineering/ai-review.yaml   # edit repos:, model:
python3 deploy/ci-review/ai-review.py review --repo berlinguyinca/herdr-engineering --pr 160 \
    --config ~/.config/herdr-engineering/ai-review.yaml --dry-run      # read + review, post nothing
python3 deploy/ci-review/ai-review.py review --repo berlinguyinca/herdr-engineering --pr 160 \
    --config ~/.config/herdr-engineering/ai-review.yaml --dry-run --max-chunks 1   # try one chunk
python3 deploy/ci-review/ai-review.py review --repo berlinguyinca/herdr-engineering --pr 160 \
    --config ~/.config/herdr-engineering/ai-review.yaml                # post the status (and comment)
python3 deploy/ci-review/ai-review.py poll --config ~/.config/herdr-engineering/ai-review.yaml
```

Requirements:

- `gh`, signed in as an account that can post statuses and comments on the repo;
- pi's `~/.pi/agent/models.json` with the `metabolomics` provider (its `baseUrl` and `apiKey`);
- PyYAML.

To see which models can be used, list them with `GET {baseUrl}/models`, and set `reviewer.model` to one of the
exact ids.

For unattended runs, install the example user units `deploy/ci-review/herdr-ai-review.{service,timer}`
(`systemctl --user enable --now herdr-ai-review.timer`, and `loginctl enable-linger`). `poll` is idempotent: a
head that already has a terminal status is not reviewed again. Re-run a head by hand with `review`; it posts a
new status and updates the existing comment.

Statuses:

- `pending`: "Review running (model id)".
- `success`: "Approved by \<model id\>: code+security review, 0 blocking (visual review not run)".
- `failure`: "Blocked: N findings — see PR comment (model id)". The comment, marked
  `<!-- independent-ai-review -->`, lists each finding with file:line and why.

If the review itself could not run, nothing terminal is posted and the pending status stays. Causes include a
gateway error after retries, a truncated or malformed answer, and a model that is not advertised; in the last
case no pending status is posted either. `poll` retries that head once the pending status is older than
`poll.pending_stale_minutes` (120). A head that moves mid-review gets nothing; its replacement is reviewed on the
next poll.

## What it checks

- Correctness, security and tests, as the model reports them in `checked`. The status names only the aspects that
  every chunk reported.
- Every blocking finding must cite a file in the PR. A finding on a PR file whose line is not shown in the diff
  still blocks, marked "line unverified". A finding citing a file not in the PR is dropped, and the status says
  "N finding(s) dropped (cited code not in the PR)".
- Independence: the model's family must differ from every model family in the commits' `Co-Authored-By` trailers
  (default author family: claude). This holds for the configured model id and for the model id the gateway says
  answered.

## What it does NOT check

- **No visual review.** There is no screenshot or UI step. The status always says "(visual review not run)", and
  `visual_review: true` is refused. UI changes still need a human or a separate visual check.
- **No tool use, no tests run.** The model reads text and answers; it executes nothing. Running tests is CI's
  job.
- **Skipped files.** Binary, lock and generated files (`skip_globs`) are not reviewed and are listed under "Not
  checked". A PR that has only such files gets no status from this script and needs a human review.
- **Cross-chunk blind spots.** A large PR is cut into 24 KB chunks. Each chunk sees its own diff, plus the full
  text of its changed files at the head SHA: up to 12 KB per file and 24 KB per chunk, with new files excluded
  because the diff already holds them. A bug that spans files in different chunks can be missed.
- **Not a human review.** It is no substitute for one, and it does not approve the PR in GitHub's review sense.

## Limits: advisory strength

- **Prompt injection.** The diff, the PR title and body, and the file contents are untrusted. The model has no
  tools, files, network or key, so an injection can only skew its verdict text. The mitigations:
  - the content sits in the user message between random fences, labelled as data, and never in the
    instructions;
  - the answer must match a strict schema, or the run fails;
  - the poster decides the state, drops findings on files outside the PR (and says so), and lets the model narrow
    its claims but never widen them;
  - one failed chunk fails the whole run.

  None of this stops a successful injection from producing "0 blocking". Treat `success` as "an independent
  model found nothing", not as proof.
- **The key lives on the host that runs the script.** It is read from `~/.pi/agent/models.json` and sent only
  as the `Authorization` header, to that provider's https `baseUrl` host. Redirects are not followed. The key is
  redacted from every message.
- **Plain http to a tailnet node is an explicit opt-in.** `reviewer.allow_plain_http_hosts` takes exact IPv4
  literals in `100.64.0.0/10`, for example `["100.104.39.6"]` for pi's `development` provider
  (`http://100.104.39.6:8390/v1`). Hostnames, IPv6, public addresses and other private ranges are refused, and so
  is a tailnet IP that is not listed. The default is empty: https only. With a listed host:
  - the key, the diff and the verdict cross the tailnet as plain HTTP. WireGuard still encrypts the link between
    the two machines, but anything on either end can read them: a proxy or sniffer on the reviewing host, the
    node's own process and logs, and any tailnet member that the ACLs let reach that port;
  - a tailnet address is only as trustworthy as the tailnet's membership and ACLs. If another device takes over
    that IP, it receives the key.

  It is acceptable when the node is a lab machine on our own tailnet, the key is a per-node key for that gateway
  only (not the `llm.metabolomics.us` key), and https is not available. One example is the 2026-10-23 review of
  PR #160 while `llm.metabolomics.us` was down. It is not acceptable for a key that also opens a public gateway,
  or for an IP the tailnet ACLs expose to devices we do not run. Remove the entry when https is back.
- **The gateway is shared, and its capacity comes and goes.** Chunks run one at a time. Measured on 2026-10-22
  against PR #160 (40 files, 36 chunks at 24 KB):
  - with thinking off (the default), deepseek answered in 15 to 31 s per chunk;
  - with thinking on, it spent all 32,768 tokens reasoning and never answered (`finish_reason=length`);
  - a 64 KB prompt was still reasoning after 13 minutes;
  - routes sometimes ended mid-answer (`upstream_transport_error`), and qwen's single slot answered 429;
  - for a while the gateway advertised no models at all.

  Route failures, 429s and 5xx responses are retried with backoff. A stream that closes unfinished (no `[DONE]`,
  no `finish_reason`) is retried the same way. A model that is not advertised fails the run before anything is
  posted.
- **qwen on the `development` provider.** That provider sets `compat.thinkingFormat: qwen-chat-template`. pi then
  sends `chat_template_kwargs.enable_thinking`, which is the switch this script already sends, so no other switch
  is needed. Measured on 2026-10-23 against qwen3.8-27b: 0 reasoning tokens with the switch, and about 670
  characters of reasoning without it. A top-level `enable_thinking: false` was ignored. The node answers under
  its own id, `qwen3.8-27b-vision-c120k-2x`, not the requested
  `qwen3.8-27b-modality-text-ctx-c128k-quant-q4_k_m`. The family is still qwen, and the status names the id
  that answered.
