# ci-review: the `Independent AI review` status

The repository ruleset requires a commit status named exactly `Independent AI review`. `ai-review.py` posts it.
Before this script, the operator posted it by hand. Operator guide: `docs/operations/independent-ai-review.md`.

```
ai-review.py review --repo OWNER/NAME --pr N [--config FILE] [--dry-run]
ai-review.py poll   [--repo OWNER/NAME ...] [--config FILE] [--dry-run]
```

Files:

- `ai-review.example.yaml`: every configuration key and its default. Configuration is YAML plus CLI flags; there
  are no environment variables.
- `herdr-ai-review.service` and `herdr-ai-review.timer`: example `systemd --user` units that run `poll` every 5
  minutes. Nothing installs them.

## What one review does

1. **Pins the head.** It reads the PR (`gh pr view`), including its head SHA and the commit messages. It reads
   the diff (`gh pr diff`), then reads the head again. If the head changed, it stops. It reads the head once more
   before the comment and once more before the status. If the head moved at any point, it posts nothing for either
   SHA; the next `poll` reviews the new head. The checkout is `gh api repos/R/tarball/SHA`, which is exact.
2. **Checks independence.** The author families come from the `Co-Authored-By:` trailers: Claude/Anthropic →
   `claude`, Codex/OpenAI/GPT → `codex`, Gemini → `gemini`. With no trailer, the author is
   `author_default_family` (`claude`). It refuses to run, posting nothing (exit 2), in these cases:
   - `reviewer.family` is one of the author families;
   - the command's executable (`codex`, `claude`, `gemini`) belongs to a different family than `reviewer.family`;
   - `reviewer.family` is empty.
3. **Chunks the diff.** Files are grouped in path order under `chunk_bytes` (60 KB). A larger file is cut at hunk
   boundaries, then at line boundaries, and each piece keeps the file header and gets correct `@@` line numbers.
   Binary files and files matching `skip_globs` (locks, minified, generated, images, archives) are not sent. Each
   one is listed under "Not checked".
4. **Reviews each chunk in the sandbox** (below). The prompt goes on stdin and contains the PR title, the PR body
   and the chunk, between two random fence lines, with the instruction to treat that text as data. The reviewer
   writes `verdict.json` in its workdir. codex is given `--output-schema verdict.schema.json`, a strict JSON
   Schema.
5. **Parses each verdict strictly.** The verdict must be one JSON object with exactly `blocking`, `minor`,
   `checked` and `not_checked`; one whole-answer `` ```json `` fence is tolerated. Each finding has exactly
   `file`, `line` (an integer), `summary` and `why`, and `checked` must include `code`. Any other answer, a
   non-zero exit, a missing verdict file, a timeout or a sandbox error fails the whole run: exit 1, and nothing is
   posted except the earlier pending status.
6. **Decides the state (the poster, not the model).**
   - A blocking finding counts only if its `file` is in the diff and its `line` is a new-side line of a hunk (an
     added or context line; for a deleted file, an old-side line). Others are dropped, counted in the status and
     listed in the comment.
   - `success` means every chunk completed and no counted blocking finding remains. Otherwise the state is
     `failure`.
   - The claims in the status are the aspects that every chunk listed in `checked`, intersected with
     `code, security, tests, docs, performance`. The model can narrow the claims but never widen them. `visual` is
     never claimed: there is no visual step, and `visual_review: true` is refused.
7. **Posts.**
   - First, a `pending` status, "Review running (label)", for the pinned SHA. Set `post_pending: false` to turn
     it off.
   - Then one terminal status, for example `Approved by codex: code+security review, 0 blocking (visual review not
     run)` or `Blocked: 3 findings — see PR comment (codex)`. It is at most 140 characters.
   - The PR comment starts with `<!-- independent-ai-review -->`. It is posted when there are blocking or dropped
     findings, and it is updated in place on later runs, including a run that comes back clean.
   - Only a comment by the `gh` user that starts with the marker is updated, so a planted marker does nothing.
   - Model text in the comment is put on one line and HTML-escaped. Markdown and links are escaped, and
     `@mentions` are broken. All bodies go to `gh api --input -`, never into argv.

`--dry-run` makes the same reads, runs the reviewer and prints the result as JSON. It posts nothing, not even the
pending status.

`poll` lists the open PRs of each repo. A head is skipped if its latest `Independent AI review` status is
`success`, `failure` or `error`, or if it is `pending` and younger than `poll.pending_stale_minutes`, which means
another run is working or a run crashed recently. Every other head is reviewed, up to `poll.max_per_run`. One
PR's failure does not stop the others, and the exit code is 1 if any PR failed. Each PR has its own `flock`, and
so does `poll`, so overlapping timer runs never review the same PR twice.

Exit codes: 0 the review completed; 1 the review failed; 2 refused; 3 the head moved; 4 another run holds the PR.

## The sandbox, and what it does not isolate

Each chunk runs `collab-sandbox.sh WORKDIR COMMAND` (bubblewrap). The workdir is a fresh
`~/herdr-agents/work/ai-review/<repo>-pr<N>-<sha>-*/cNNN/`. Inside the box:

- **Files.** The system is read-only, and the home directory is an empty tmpfs.
  - The checkout of the head SHA is a sibling directory, shown read-only (`HERDR_SANDBOX_RO_BINDS`). It is
    unpacked with `tarfile` `filter="data"`, so no links out, no device files and no absolute paths.
  - The harness directory (`HERDR_SANDBOX_HARNESS_DIR`, mode 0700) holds only `codex/auth.json`, copied from
    `~/.codex`. The operator's `~/.codex/config.toml`, `~/.claude*`, `~/.pi`, ssh keys, gh login and cloud
    credentials are not in the box.
  - The run's directories are removed afterwards, unless `keep_workdir: true`.
- **Network** (`sandbox.network: egress`, the default). The box has its own network namespace with only
  loopback. `collab-egress.py serve` runs on the host and tunnels only the `host:port` pairs in
  `sandbox.egress_allow`. For codex the default is `chatgpt.com:443`, `api.openai.com:443` and
  `auth.openai.com:443`. Everything else is refused and logged to the run's `blocked.jsonl`. No service keys
  are bound, and GitHub is not reachable, so the reviewer cannot post anything itself.
- **Limits.** The box has its own PID namespace, no capabilities, a cleared environment, and process/memory caps
  (a systemd user scope).

What is NOT isolated, or not verified yet:

- **The model endpoint is open, and the login is in the box.** The reviewer can send anything it read (the diff
  and the public checkout) to the allowed hosts. Injected text can get it to do that, and it can also use or
  exfiltrate the login token through those same hosts. The token is the operator's codex login. Nothing more
  sensitive than the PR itself is in the box.
- **Unverified (no real codex run yet):**
  - That codex honours `HTTPS_PROXY`. It is a Rust client and should.
  - That the default allow list is complete for a ChatGPT login.
  - That codex's own `-s read-only` sandbox works nested inside bwrap.

  For the first real run, set `keep_workdir: true`, then read `~/.local/state/herdr-ai-review/run-*/blocked.jsonl`
  and `proxy.err`. If codex's inner sandbox fails nested, `-s danger-full-access` is acceptable inside this box,
  because the box is the boundary, but decide that deliberately.
- **`sandbox.network: host`** gives the box the host network. Do not use it without a reason.
- **Prompt injection can still win.** A diff that persuades the model to report no blocking findings gets a
  `success`. The poster's checks limit what an injection can do, but they cannot make the model honest:
  - fake findings are dropped;
  - a "do not check X" instruction shows in `checked`;
  - malformed output never passes.

  The status is advisory-strength; see the operations doc.
