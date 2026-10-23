# ci-review: the `Independent AI review` status

The repository ruleset requires a commit status named exactly `Independent AI review`. `ai-review.py` posts it.
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
- It is never written to a file, argv, the environment, a log, a prompt, a comment or a status. It is redacted
  from every message the script prints, including gateway error bodies that echo it.
- The baseUrl must be `https://host[:port]/path`, without credentials or a query. A provider with custom
  `headers` is refused.

**One request.**
- There are two messages. The `system` message holds the instructions. The `user` message holds everything from
  the PR between two random fence lines: title, description, file list, diff, and the full-file context.
- `response_format` is `json_schema`, the strict verdict schema; the gateway accepts it.
- `max_tokens` is 32768, because reasoning models think first.
- `stream: true`. The gateway ends a request that produces no tokens for a while (`inference_idle_timeout`), and
  reasoning tokens keep a slow answer alive. Only `content` deltas count as the answer; `reasoning_content` is
  ignored.
- `finish_reason=length` or an empty answer is retried once, then the chunk fails.
- 429, 5xx and connection errors are retried 4 times, with a backoff of 5 s doubling each time. A `Retry-After`
  header wins, capped at 60 s.
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
   - `checked` must include `code`. For a chunk of prose only (`.md`, `.rst`, `.txt`, `.adoc`) it must include
     `docs` instead, and that chunk counts as covering code and tests, since it has none.
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
