# ci-review: the `Independent AI review` status

> As of 2026-11-13 the required status is `Opus agent review`, posted by a Claude session; this script's status is advisory. See `docs/operations/independent-ai-review.md`.

The repository ruleset used to require a commit status named exactly `Independent AI review`. `ai-review.py` posts it.
Before this script, the operator posted it by hand. Operator guide: `docs/operations/independent-ai-review.md`.

```
ai-review.py review --repo OWNER/NAME --pr N [--config FILE] [--dry-run [--max-chunks K]]
ai-review.py poll   [--repo OWNER/NAME ...] [--config FILE] [--dry-run]
```

Files:

- `ai-review.example.yaml`: every configuration key and its default. Configuration is YAML plus CLI flags; there
  are no new environment variables.
- `herdr-ai-review.service` and `herdr-ai-review.timer`: example `systemd --user` units that run `poll` every 5
  minutes. Nothing installs them.

## The reviewer

The reviewer is a model that the pi provider's OpenAI-compatible gateway advertises. By default that is
`llm.metabolomics.us`, through the `metabolomics` provider in `~/.pi/agent/models.json`. The script calls
`POST {baseUrl}/chat/completions` itself: one request per chunk, no tools, no files, no harness.

The model is chosen with `reviewer.model` and must be an exact id from `GET {baseUrl}/models`. The default is
`deepseek_v4-flash-modality-text-quant-mxfp4`. It is checked as advertised before anything is posted, and its
family (deepseek, qwen, ...) must differ from the author's.

**The key.** The script reads `apiKey` from that provider. It accepts a literal or `$NAME`/`${NAME}`; a `!command`
is refused, and nothing from models.json is run.
- It is sent only as `Authorization: Bearer …`. That is what pi's OpenAI client sends, whether or not
  `authHeader` is set.
- It goes only to the `baseUrl` host, over https with TLS verified. `http.client` is used, so no redirect is
  followed (a 3xx fails the chunk) and no proxy from the environment is used.
- Plain http is an explicit opt-in: `reviewer.allow_plain_http_hosts` lists exact IPv4 literals in the tailnet
  range `100.64.0.0/10`. Only a baseUrl `http://<listed ip>[:port]/path` is then accepted. A hostname, IPv6, a
  public address, another private range (10/8, 172.16/12, 192.168/16, loopback) or a non-canonical literal is
  refused at config load; a tailnet IP not in the list is refused too. The default is an empty list: https only.
  See `docs/operations/independent-ai-review.md` for what that costs.
- It is never written to a file, argv, the environment, a log, a prompt, a comment or a status. It is redacted
  from every message the script prints, including gateway error bodies that echo it.
- The baseUrl must be `https://host[:port]/path` (or the allowed `http://` form above), without credentials or a
  query. A provider with custom
  `headers` is refused.

**One request.**
- There are two messages. The `system` message holds the instructions. The `user` message holds everything from
  the PR between two random fence lines: title, description, file list, diff, and the full-file context.
- `response_format` is `json_schema`, the strict verdict schema; the gateway accepts it.
- `max_tokens` is 32768. Thinking is off by default (`reviewer.thinking: false` sends
  `chat_template_kwargs.enable_thinking=false`). With thinking on, deepseek spent all 32768 tokens reasoning on
  24 KB chunks and never answered (`finish_reason=length`); with it off, it answered in 15 to 30 s per chunk.
  The same switch is what pi sends for `compat.thinkingFormat: qwen-chat-template`; on the `development`
  provider's qwen3.8-27b (llama.cpp) it gave 0 reasoning tokens, while a top-level `enable_thinking: false` was
  ignored (2026-10-23).
- A stream that closes with neither `[DONE]` nor a `finish_reason` (seen: a node answering 200 with an empty
  stream) is retried like a 5xx.
- `stream: true`. The gateway ends a request that produces no tokens for a while (`inference_idle_timeout`), and
  reasoning tokens keep a slow answer alive. Only `content` deltas count as the answer; `reasoning_content` is
  ignored.
- `finish_reason=length` or an empty answer is retried once, then the chunk fails.
- 5xx and connection errors are retried 4 times, with a backoff of 5 s doubling each time. A `Retry-After`
  header wins, capped at 60 s.
- 429 means the model's single slot is taken (deepseek and qwen serve one request at a time; another review may
  hold the slot for minutes). It is waited out separately, for up to `reviewer.busy_wait_s` (default 1800 s) in
  total, and does not use up `retries`. If the gateway stays busy past that, the run fails and posts no terminal
  status; `poll` retries the head once its pending status goes stale.
- The answer's `model` field must also be of an independent family. The status names that id.
- Chunks run one at a time (`reviewer.parallel: 1`), because the gateway is shared.

## What one review does

1. **Pins the head.** It reads the PR (`gh pr view`), including its head SHA and the commit messages. It reads
   the diff (`gh pr diff`), then reads the head again. If the head changed, it stops. It reads the head once more
   before the comment and once more before the status. If the head moved at any point, it posts nothing for either
   SHA; the next `poll` reviews the new head.
2. **Checks independence.** The author families come from the `Co-Authored-By:` trailers, using the same table as
   the model ids: claude, codex/openai, gemini, qwen, deepseek, llama, mistral, glm, kimi. With no trailer, the
   author is `author_default_family` (`claude`). The run is refused, posting nothing (exit 2), when the model's
   family is unknown or is one of the author families.
3. **Chunks the diff.** Files are grouped in path order under `chunk_bytes` (24 KB). A larger file is cut at hunk
   boundaries, then at line boundaries, and each piece keeps the file header and gets correct `@@` line numbers.
   Binary files and files matching `skip_globs` (locks, minified, generated, images, archives) are not sent. Each
   one is listed under "Not checked". A diff with nothing left to review is never approved: the run fails and
   nothing is posted.
4. **Adds context.** For each changed file in a chunk, it adds the file's full text at exactly the head SHA
   (`gh api …/contents/PATH?ref=SHA`). New files are skipped, because the diff already holds all of them.
   - Each file is cut at `context_bytes_per_file` (12 KB), and a chunk's context at `context_bytes_per_chunk`
     (24 KB), so a request stays at about 48 KB plus the PR body. That is about 15k tokens, well inside the
     262,144-token context of one slot. The real limit is time: a reasoning model's thinking grows with the
     prompt. On the gateway, 16 KB prompts answered in 1 to 3 minutes, while a 64 KB prompt was still reasoning
     after 13 minutes.
   - A file that cannot be fetched is noted under "Not checked"; it does not fail the run.
5. **Parses each verdict strictly.**
   - The answer must be one JSON object with exactly `blocking`, `minor`, `checked` and `not_checked`; one
     whole-answer `` ```json `` fence is tolerated. Each finding has exactly `file`, `line` (an integer),
     `summary` and `why`.
   - `checked` must include `code`. A chunk with no source code may report `docs` instead, and counts as covering
     code and tests, since it has none. That means only prose and config files: `.md`, `.rst`, `.txt`, `.adoc`,
     `.yaml`, `.yml`, `.toml`, `.json`, `.ini`, `.cfg`, `.conf`, `.service`, `.timer`.
   - Anything else fails the whole run: exit 1, and nothing is posted except the earlier pending status.
6. **Decides the state (the poster, not the model).**
   - A blocking finding whose `file` is in the diff always counts. If its `line` is not a new-side line of a hunk
     (an added or context line; for a deleted file, an old-side line), it still blocks and the comment marks it
     "line unverified".
   - Only a blocking finding whose `file` is not in the PR at all is dropped. It is listed in the comment, and the
     status says "N finding(s) dropped (cited code not in the PR)".
   - `success` means every chunk completed and no blocking finding remains. Otherwise the state is `failure`.
   - The claims in the status are the aspects that every chunk listed in `checked`, intersected with
     `code, security, tests, docs, performance`. The model can narrow the claims but never widen them. `visual` is
     never claimed: there is no visual step, and `visual_review: true` is refused.
7. **Posts.**
   - First, a `pending` status, "Review running (model id)", for the pinned SHA. It is posted only after the
     model is confirmed as advertised.
   - Then one terminal status that names the exact model id, at most 140 characters. Examples:
     `Approved by deepseek_v4-flash-modality-text-quant-mxfp4: code+security review, 0 blocking (visual review not
     run)` and `Blocked: 3 findings — see PR comment (model id)`. When space runs out, the claims list goes first;
     the model id, "0 blocking", the dropped count and the visual caveat always stay.
   - The PR comment starts with `<!-- independent-ai-review -->`. It is posted when there are blocking or dropped
     findings, and it is updated in place on later runs, including a run that comes back clean.
   - Only a comment by the `gh` user that starts with the marker is updated, so a planted marker does nothing.
   - Model text in the comment is put on one line and HTML-escaped. Markdown and links are escaped, and
     `@mentions` are broken. All bodies go to `gh api --input -`, never into argv.

`--dry-run` makes the same reads and model calls and prints the result as JSON, including the model, each chunk's
latency and the findings. It posts nothing, not even the pending status. `--max-chunks K` works only with
`--dry-run`: it reviews only the first K chunks, and the result's state is `partial`, never success.

`poll` lists the open PRs of each repo. A head is skipped if its latest `Independent AI review` status is
`success`, `failure` or `error`, or if it is `pending` and younger than `poll.pending_stale_minutes`, which means
another run is working or a run crashed recently. Every other head is reviewed, up to `poll.max_per_run`. One
PR's failure does not stop the others, and the exit code is 1 if any PR failed. Each PR has its own `flock`, and
so does `poll`, so overlapping timer runs never review the same PR twice.

Exit codes: 0 the review completed; 1 the review failed; 2 refused; 3 the head moved; 4 another run holds the PR.

## Always-on review (two tiers, pluggable backends)

`always-on-review.py` extends this reviewer with two tiers and a choice of backend per tier. It reuses this file's
diff parsing, chunking, strict verdict schema, finding validation, stale-pending retry and "the script, not the model,
decides the state" rule. `ai-review.py` itself keeps working unchanged (the gateway backend is the same call).

| Tier | Default context | Backend (config) | When it runs | Required? |
|---|---|---|---|---|
| tier1 | `Open-weight pre-review` | `pi` with an open-weight alias | every non-draft head | no, advisory |
| tier2 | `Opus agent review` | `claude` with `opus` | every non-draft head, without waiting for checks; with `gates.wait_for_green: true`, only when every `gates.required` name is success on that exact SHA and every existing `gates.when_present` check is success | yes, once the ruleset is changed by the operator |

Backends (`tiers.<t>.backend`, settings under `backends.<name>`):

- `gateway`: the OpenAI-compatible call, chunked as in ai-review.
- `claude`: `claude -p --model <model>` in a fresh detached worktree of the PR head. Tools are exactly `Read`,
  `Grep` and `Glob`: no Bash and no git allow rules (a `git diff:*` rule is not read-only, since
  `--output=<path>` writes a file). The diff (cut at `diff_bytes`) and the commit log are in the prompt; the worktree
  is there for reading the code around the change. `--permission-mode dontAsk`, `--setting-sources ""` (measured: with
  `--setting-sources user` a permissive user rule let `Read` leave the worktree; with `""` it is denied and a logged-in
  CLI still works), no MCP, no slash commands, no session persistence, strict `--json-schema` verdict, hard time cap.
  The prompt text and the worktree are untrusted input; for host-level isolation set `backends.claude.wrapper` to a
  sandbox prefix such as `bwrap` (optional, not installed or required by this script).
- `pi`: `pi -p --model <provider/alias>` with `--tools read,grep,find,ls`, no extensions, skills, MCP, context files
  or session, `--offline`. pi has no JSON-schema flag, so the diff is in the prompt and the answer is parsed strictly
  from stdout. pi has no file sandbox and can read any file the user can, so its answer is not trusted to be free of
  secrets: the pi provider key and the `gh auth token` value are added to the redaction set (best effort, exact
  strings only: other secrets are NOT covered), and a tier that uses pi with `comment: true` is refused unless
  `backends.pi.wrapper` is set (for example a `bwrap` prefix). Statuses and `reviews.jsonl` are redacted the same way.

### Review prompt v2, passes, evidence and replay

- `prompt.project_rules` (list of strings) and `prompt.checklist_file` (`review-checklist.yaml`: categories code, security, tests, docs, performance, migrations; each item an `id` and a one-line `question`; unknown keys refused) go into both tiers' system prompt. The reviewer names the ids it considered in `checked_items`.
- Evidence rule: a blocking finding carries `evidence` (text quoted from the diff or the file) and `how_it_fails`. The script, not the model, downgrades it to a note when `how_it_fails` is empty or the evidence is not a meaningful quote: at least 20 normalised characters, or (shorter) a whole line of at least 12 characters (a trivial line such as `except Exception as e:`, `from __future__ import annotations` or `raise NotImplementedError()` is never evidence, of any length), found in the added, context or removed lines of the cited file's hunks (for a deleted file, its removed lines), or in that file at the pinned head. Diff `+`/`-` markers on the quote and an `a/`, `b/` or `./` prefix on the path are ignored; a quote from another file's hunk does not count. A blocker citing a file that is not in the PR is not downgraded: it is dropped (`N finding dropped`), and a `block` verdict whose only blockers were dropped does not count as unverified. The downgrade count goes into the status description (`N downgraded`) and the failure comment. If the model said `block` and every blocker was downgraded, tier 2 does NOT post success: it stays pending (logged as `unverified` with the findings), while tier 1 (advisory) posts success noting `N unverified blockers downgraded`. Disable with `prompt.require_evidence: false`.
- `tiers.tier2.passes: [{name, focus}]`: one claude run per pass, in order, focus appended to the prompt; findings are merged (deduped by file, line, category and the start of the reason; the one with evidence or higher severity is kept) and any surviving blocker blocks. The time cap is shared: one `time_cap_s` covers all passes, each pass may use an equal share of the time left (unused time passes on). If a pass fails or times out after another completed, the completed passes' findings are kept and logged with `partial` set: a surviving blocker still posts failure, otherwise the status stays pending, never success. Size `time_cap_s` for all passes together (the example uses 3600 for four). The one-review lock covers the whole review. `passes: []` or one pass behaves like a single review.
- `reviews.jsonl`: each completed review line has `downgraded`, `passes` (name, duration_s, cost_usd, turns when the backend reports them), `checked_items`, and `findings`: up to 20 of `file, line, severity, category, why, evidence, how_it_fails`, each text field cut at 600 chars and passed through `redact()`.
- Replay: `always-on-review.py replay --corpus DIR --tier tier1|tier2 --config YAML [--limit N] [--out FILE]` checks out each case's head in a detached worktree, runs the tier read-only and posts nothing (no gh client is built; posting functions refuse inside replay). The corpus is one JSON per case: `{repo, pr, head_sha, base_sha?, source: opus|human|revert|follow-up-fix, notes, ground_truth: [{id, file, line|null, category, summary, severity, confirmed?}], known_false_claims?}`. Scoring: FOUND (same file and line within 10, or two shared keywords), PARTIAL (file only), MISSED; a blocking finding matching no ground truth is FALSE_BLOCKER_CANDIDATE (unconfirmed), one matching a known false claim is FALSE_BLOCKER. The report gives per-case and aggregate recall, false-blocker rate, per-category recall, cost and duration. `replay compare a.json b.json` compares two runs.
- Intended corpus: thousands of PRs across the org's repos. Replay is resumable (cases already scored in `--out` are skipped; failed and skipped ones are retried, and a case whose head or ground truth changed is scored again) and runs one case at a time (a lock refuses a second replay). `corpus build --repo OWNER/NAME --since YYYY-MM-DD --out DIR` derives cases from merged PRs with read-only `gh` calls (reverts, follow-up fixes, review comments). Only thread-root comments by humans, or by logins listed in `replay.reviewers` (pass `--config`), and not by the PR author, count; the case is pinned to the commit most comments were made on (`original_commit_id`) and only those comments keep their line; its ground truth is `confirmed: false` and is not scored until confirmed.

### Choosing a pi model or alias

pi "aliases" are the gateway aliases listed as model entries under a provider in `~/.pi/agent/models.json` (for
example provider `metabolomics`, ids `qwen3.8-27b`, `qwen3.8-flash`, ...). There is no separate alias file. List them
with `pi --list-models [search]`, then set `tiers.tier1.model: metabolomics/<alias>` (optionally `:<thinking>`).
`PI_CODING_AGENT_DIR` is not used; the default `~/.pi/agent` applies.

### Dry-run is the default; two keys to post

Nothing is posted unless the config has `post: true` AND the command has `--post`. Without both, reviewers still run
and `state_dir/reviews.jsonl` gets one line per review, but no status or comment is written to GitHub.

```
always-on-review.py show-command --tier tier2 --config CFG     # print the exact reviewer command line
always-on-review.py review --tier tier1 --repo O/R --pr N --config CFG           # dry run
always-on-review.py poll --config CFG                                            # dry run of the whole loop
always-on-review.py poll --config CFG --post                                     # posts only if CFG has post: true
```

Gates: by default tier 2 does not wait for green checks, so Opus can review every pushed head. Set
`gates.wait_for_green: true` (YAML only; it must be a boolean) to restore the green-only gate, and then
`gates.required` is mandatory. With it on, `gates.required` names must be present and `success`; `neutral` and `skipped` do not count as green. A
`gates.when_present` name (a pipeline that only runs on some PRs, such as `ui-gates`) may be absent or neutral/skipped,
but when it exists it must be `success`: pending or failed blocks tier 2.

Use a dedicated bare clone for `clone:` (not your working checkout): the script fetches `refs/pull/N/head` into
`refs/herdr-review/*` there and removes those refs and the worktree after each review.

Guarantees: one review per exact head SHA (idempotent, redone on every push), drafts skipped, one review at a time,
per-review time cap (process group killed), cooldown after repeated failures, status stays `pending` whenever a
review cannot run or its answer fails the schema (`post_status` refuses success without a parsed verdict for that
SHA), blocking findings must cite a file in the PR (others are dropped), and a failure posts one marked comment
(`<!-- always-on-review:<slug> -->`, updated in place). The reviewer subprocess gets a scrubbed environment (no
`GH_TOKEN`, no API keys) and the PR title, body and diff are treated as untrusted. "No network" means no network
tools; the CLIs still reach their own model API.

Examples (not installed by anything): `always-on-review.example.yaml`, `herdr-always-on-review.service` / `.timer`
(systemd --user), and `herdr-review-pane.example.sh` (a herdr pane that tails `reviews.jsonl`).

## Limits

- **Prompt injection can still win.** The model has no tools, filesystem, network or key, so text in a PR can
  only skew the verdict text. A diff that persuades the model to report no blocking findings gets a `success`.
  The poster's checks limit what an injection can do, but they cannot make the model honest:
  - findings citing files outside the PR are dropped, and the status says so;
  - an instruction like "don't check X" shows up in `checked`;
  - malformed output never passes.
- **Cross-chunk blind spots.** Each chunk sees its own diff, plus the changed files' full text up to the caps
  above. It does not see the rest of the PR or unchanged files.
- **The key lives on the host that runs the script.** That is the operator's pi config. Anyone who can read that
  file there can use the gateway.
- **The gateway decides how long a request takes.** It is shared, and a large prompt can queue or think for
  minutes. See the operations doc for measured latencies.
