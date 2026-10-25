# CI fix agent design

Status: draft for operator review, 2026-10-24. Statements marked **(guess)** were not verified in code or live data and are listed as spikes at the end.

## Goal

The operator's request, in their words: "click on a failed CI run and say 'open terminal, attach to herdr in new workspace/tab, start pi with available model in sandbox, clone this project and checkout this branch in a worktree, then diagnose the issue and try to fix it using LLM. When the agent quits, everything is cleaned up again. Last but not least it like always needs to register under agents."

So one click on a failed run starts a **throwaway, sandboxed Pi agent** that works on an isolated copy of the failing commit, can be watched or attached to like any other agent, produces a reviewable result, and leaves nothing behind.

Non-goals and anti-goals:

- The agent never merges, never pushes to the failing branch, and never holds a push credential.
- No agent starts by itself. A person clicks (automatic start on every failure is out of scope).
- The control plane never starts a process (the internet-facing and dashboard services have no way to). A host-side runner does, as the collaboration fleet runner already does.

## What exists today

| Piece | Where | Use here |
| --- | --- | --- |
| Fix with Pi / Send to Pi | `control_plane/ci_debug.py`, `#/ci` (PR #215) | Sends the failure context to a Pi session **you already have**. Stays, for that case. Its context builder (repository identity check, exact full commit, redacted bounded log summary, untrusted-data framing, idempotent replay) is reused unchanged. |
| Host-side runner pattern | `deploy/control-plane/collab-spawn.py`, systemd timer, `collab-sync.sh` | The control plane only records a request; a host process claims it once, starts a herdr workspace over `herdr [--machine H]`, tracks it through state files, and cleans up. Probes which hosts are up and have the harness CLI. Revalidates every name that reaches a command line. |
| Sandbox | `deploy/control-plane/collab-sandbox.sh` (bubblewrap) | Read-only system, empty tmpfs home, one writable work directory under `~/herdr-agents/work/`, harness login visible read-only, process and memory caps, no ssh keys. With an egress socket the box has **no network of its own**, only a relay to an egress proxy that allows named hosts (`collab-egress.py`). |
| Helper supervision | `helper_run`, the egress proxy, the watch loop | Deadline, stop on cancel, output cap, remove work dir and state on exit. |
| herdr CLI | `herdr worktree create/open/remove`, `workspace create`, `pane run`, `agent start/prompt/wait/attach` | A worktree-backed workspace, an interactive agent started in a pane, waiting for its state, attaching. |
| Agents registration | feed plugin (`deploy/herdr-cp-feed-plugin`), session ids `herdr:<host>:<server>:<pane>` | Any pane in which herdr detects an agent appears under Agents. |
| Terminal button | `control_plane/terminal_attach.py` (PR #202) | Gives the viewer the exact `herdr ... agent attach` command for the session. The browser cannot open a terminal itself. |
| LLM page | PRs #211 to #213 | Usage of the fix agent shows up by session id, with its cost, speed and problems. |

## Findings that shape the design

1. **The sandbox deliberately has no credentials.** No ssh keys, no tokens, a tmpfs home. So the agent can neither `git clone` a private repository nor push. This is a feature: the clone and the push happen **outside** the box, by the runner, which is the only holder of forge credentials.
2. **A git worktree does not fit the box.** A worktree's `.git` file points at the main repository outside the work directory, which the box cannot see. The box therefore gets a **standalone clone** (objects copied, no alternates) at the exact commit. `herdr worktree` stays an option once the box can bind the git directory (spike 3).
3. **Registration is free but incomplete.** herdr detects the agent in the pane and the feed plugin reports it, so it appears under Agents. What the agent cannot do from inside the box is tell the control plane which CI run it belongs to, so the runner records that association itself.
4. **Transcripts may be lost.** The box has a tmpfs home, so Pi's session file would vanish with the box, and the feed plugin reads transcripts from the host (spike 2).
5. **A CI run on the resulting pull request can run attacker-influenced code with CI secrets.** The agent's output is derived from untrusted logs and a possibly hostile repository. See "Security".

## Proposal

### Flow

```
dashboard  --(1) POST /api/v1/ci/fix {repository, number, operation_id}-->  control plane
control plane (2) revalidates the run like Send to Pi, records CIFixRequested (state queued)
host runner   (3) claims it, picks a host and a model, fetches the repo (host credentials) into a mirror
              (4) makes the work copy at the exact commit on a new branch, in ~/herdr-agents/work/ci-fix/<id>/
              (5) opens a herdr workspace/tab, starts sandboxed Pi there with the fix prompt
              (6) records the association (run <-> session <-> branch) and state running
operator      (7) watches it on Agents, or attaches with the Terminal button
agent quits   (8) runner collects: commits? bounded diff? policy checks
              (9) pushes branch herdr-ci-fix/<run>-<sha7>, opens a DRAFT pull request linking the run
              (10) removes the work copy, closes the workspace, stops the proxy, deletes the harness dir
              (11) records CIFixFinished with a bounded result (PR url, diff stat, summary)
```

States: `queued` → `claimed` → `preparing` → `running` → `finishing` → `succeeded` (a PR exists) | `no_change` (the agent ended without a commit) | `failed` | `cancelled` | `expired`. Each transition is a durable event, so the dashboard and a restarted runner agree.

### Request (control plane)

`POST /api/v1/ci/fix` is operator-only and idempotent on `operation_id`. It reuses `ci_debug`'s run validation: the provider identity, an exact full commit, a redacted and bounded failing step and log summary. It adds:

- **One active fix per `(repository, run)`.** A second request returns the first one's state instead of starting another.
- **Global and per-host caps** from YAML. A request over the cap stays `queued` with a visible reason; it does not fail.
- The request stores only ids, the commit, the branch name and the bounded context, never credentials.

`GET /api/v1/ci/fix/<id>` returns the state, reason, session id, branch, PR link and result. `POST .../cancel` is operator-only and makes the runner stop and clean up.

### Runner (host side)

A new `deploy/control-plane/ci-fix-runner.py` (timer, installed like `collab-sync.sh install`), built on the helper runner's patterns rather than a copy of its code where possible:

- **Host choice.** Hosts come from YAML (`ci_fix.hosts`), probed each cycle: reachable over herdr, `pi` and `bwrap` present, free slot, free disk above a floor. Round robin among eligible hosts, like the fleet runner. None eligible: stay `queued` with the reason.
- **Model choice ("available model").** The first entry of the YAML preference list that the gateway catalogue currently lists (the runner already posts the catalogue). None listed: stay `queued` with "no model available" until a timeout, then `failed`. Reasoning models are given enough output headroom (memory `reasoning-models-need-token-headroom-to-answer`).
- **Clone.** `git fetch` into a per-repository mirror under the runner's own state (`~/herdr-agents/ci-fix/mirrors/`), using the runner's forge credential, then `git clone --no-hardlinks` from the mirror into `~/herdr-agents/work/ci-fix/<id>/repo`, `checkout` the exact commit, create the branch `herdr-ci-fix/<run>-<sha7>`. Size caps on the mirror and the copy; a commit that is not in the mirror after one fetch fails the request with that reason. The mirror's remote credential never enters the copy (the copy's `origin` is removed).
- **Launch.** A herdr workspace and tab labelled `ci-fix <repo> #<run>` (or a pane in a new tab), then `herdr agent start ci-fix-<repo>-<run> --kind pi --pane P` with the sandbox command from `collab-sandbox.sh` around Pi, `HERDR_EGRESS_SOCK` set so the box has no network but the proxy. `herdr agent prompt` delivers the fixed instructions plus the untrusted context. Every name reaching a command line is revalidated as in `collab-spawn.py`; the harness command is fixed in the runner, request text never becomes a command.
- **Egress allowlist.** Default: the LLM gateway only. Per-project YAML can add package registries for running tests. A test that needs a blocked host fails inside the box and the agent reports it.
- **Watch.** Each cycle the runner reads state files for running fixes: the agent's state through `herdr agent wait`/`get`, deadline (default 60 minutes), idle limit, cancel flag, operator revoke. An agent that quits, or a deadline that passes, moves the fix to `finishing`.
- **Reconcile.** On start the runner scans its state directory; a fix whose process is gone is finished or failed and cleaned, so a crashed runner never leaves a box, a proxy or a work copy behind.

### Result gate (the only place that pushes)

When the agent has quit, the runner, outside the box:

1. Reads the commits the agent made on the fix branch. None: `no_change`, clean up, done.
2. Checks bounds: commit count, files changed, bytes changed, no binary blobs above a size, no symlink leaving the tree, no submodule changes, no change to hook or git config files.
3. Flags (does not block) edits to CI configuration, dependency lockfiles, or anything under paths listed in YAML. A flag is shown in the PR body and the dashboard. A path list in YAML can instead mark a path as **blocking**, so a run that touches it ends `failed` with the reason and nothing is pushed.
4. Pushes only `herdr-ci-fix/<run>-<sha7>`, with the runner's credential, never to the failing branch or any protected name, and refuses if the remote already has that branch.
5. Opens a **draft** pull request against the failing branch's base, with a body that links the CI run, states that an agent wrote it, shows the bounded summary the agent printed, lists the flags, and names the session. Marked with a label (`herdr-ci-fix`) so reviewers and Woodpecker rules can recognise it. It never requests auto-merge.

### Cleanup ("when the agent quits, everything is cleaned up")

Always, on every ending (success, failure, cancel, expiry, crash recovery): close the workspace and tab, stop the egress proxy, remove the work directory and the per-run harness directory, remove the branch from the mirror unless it was pushed. What stays is the durable result record (state, reason, branch, PR url, bounded diff stat and summary, token usage via the LLM page) and the agent's session record under Agents, which is history, not residue. Tests assert that no directory, process, socket or workspace remains after each ending.

### Registration under Agents

- The agent is started with a name and `--kind pi`, so herdr detects it and the feed plugin reports it as a normal session, with role `ci-fix`.
- The runner records the association itself: an inspector `association` event for the session carrying the repository, branch, CI run and, once it exists, the PR, and the `CIFixStarted` event carries the session id. The agent in the box cannot forge this.
- The Agents list shows the fix agent like any other. The session page links to the CI run and the PR; the CI run's panel links to the fix agent (see the cross-linking design).
- Its LLM usage appears on `#/llm/session/<id>` with no extra work, because Pi sends its session id to the gateway.

### Attaching and "open terminal"

A browser cannot open a terminal. The CI run panel shows the fix agent's state and the existing **Terminal** button's two copyable commands (ssh form and local form) for its session, so the operator opens it on whichever machine they are on. While running, the panel also shows the last lines of the agent's output through the existing session workspace, not a new stream.

## API summary

Operator listener only; none of these is added to the public allowlist.

```
POST /api/v1/ci/fix              {repository, number, operation_id, provider_url?}  -> {id, state}
GET  /api/v1/ci/fix/<id>         -> {state, reason, host, model, session_id, branch, pr_url, result, started_at, ended_at}
GET  /api/v1/ci/fix?run=<repo>:<number>
POST /api/v1/ci/fix/<id>/cancel
# runner-facing, role-restricted (the runner credential, not the browser):
POST /api/v1/ci/fix/claim        -> next queued request, once
POST /api/v1/ci/fix/<id>/state   {state, reason?, session_id?, ...}
POST /api/v1/ci/fix/<id>/result  {bounded result}
```

## UI

On a failed run, next to Send to Pi and Fix with Pi: **Fix in sandbox**, with a one-line note of what it does (clones the exact commit into a throwaway sandbox on one of your hosts, runs Pi there, and opens a draft pull request if it changes anything; it never merges). While a fix exists the button is replaced by its status card: state, host, model, elapsed, a link to the agent, the Terminal commands, Cancel, and on completion the draft PR link, the flags, and the diff stat. All states (queued with reason, running, no change, failed with reason, cancelled) are explicit. Works at phone width.

## Security

- **No credentials in the box.** The box has no keys, no tokens, a tmpfs home, no network except the egress proxy to named hosts. The clone and the push use the runner's credential on the host, which the box cannot read.
- **Untrusted input everywhere.** Logs, step names, branch names, repository contents and the agent's own output are untrusted. The control plane's instructions are fixed text; context is delivered as marked evidence; nothing from them becomes a command line, a path or an `href`.
- **Prompt injection cannot reach a secret or the failing branch.** The worst an injected agent can do is write a bad commit on a throwaway branch that the runner pushes as a draft PR for review. The result gate bounds and flags that commit before anything leaves the host.
- **CI on the new branch.** Woodpecker will run pipelines for the pushed branch or PR. If the forge or Woodpecker gives same-repository PR pipelines access to repository secrets, a malicious commit could exfiltrate them from CI instead of from the box. **Release blocker (spike 5):** verify the Woodpecker setting for PR events and require approval or secret-less runs for `herdr-ci-fix/*` before this ships.
- **Blast radius caps.** Concurrency per host and globally, a per-run deadline, memory and process caps from the sandbox, a disk floor, a mirror size cap, and the one-active-fix-per-run rule.
- **Operator listener only.** A regression test asserts every `/api/v1/ci/fix*` route and the new JS return 404 on the public port.
- **Audit.** Every transition and the pushed branch are durable events with the requesting actor.
- **Same-origin guard** on the POSTs, like the summary-fix and terminal routes.

## Configuration (YAML only)

```yaml
sections:
  control_plane:
    values:
      ci_fix:
        enabled: false                  # off until the operator turns it on
        hosts: [bender, beast]          # runner-eligible; must also be reachable over herdr
        max_concurrent: {global: 4, per_host: 2}
        models: [qwen3.8-27b, deepseek-v4.1-flash]   # first one the gateway lists wins; example names
        deadline_minutes: 60
        idle_minutes: 15
        egress: {default: [gateway], per_project: {"org/app": [pypi.org, files.pythonhosted.org]}}
        limits: {max_commits: 10, max_files: 50, max_diff_kb: 512, mirror_gb: 5, free_disk_gb: 10}
        paths: {flag: [".woodpecker/**", ".github/**", "*.lock"], block: [".git*/hooks/**"]}
        pull_request: {draft: true, label: herdr-ci-fix}
```

Forge credentials for the mirror and the push are **files readable only by the runner user** referenced by path, never values in YAML and never in GitHub. Nothing reads environment variables.

## Test plan

- **Control plane:** request validation (reuses the `ci_debug` cases), idempotency, one active fix per run, caps keep requests queued with a reason, cancel, state machine rejects illegal transitions, durable replay, operator-only, public 404.
- **Runner, with fakes for herdr, git remote, the forge and the gateway:** host and model selection and the "queued, no model" path; mirror fetch and clone produce the exact commit and no `origin`; every name revalidated; a state file per fix and crash reconciliation (kill the runner mid-run, restart, nothing left behind); cancel and deadline stop the agent; cleanup after every ending (no directory, process, socket or workspace remains).
- **Result gate:** no commit gives `no_change`; bounds exceeded fails; blocking and flagged paths; symlink and submodule and hook tricks refused; only the fix branch is pushed; refuses an existing remote branch; PR is a draft with the link and flags; never merges.
- **Sandbox integration (skipped without bubblewrap):** the box cannot read the runner's credential files, has no network but the proxy, and cannot write outside the work directory.
- **Registration:** the session appears with role `ci-fix`, the association event links run, session, branch and PR, and the LLM session page shows its usage.
- **UI (node unit plus Playwright):** every status renders with its reason, the button becomes a status card, Cancel works, Terminal commands copy, links reach the agent and the PR, 390 px width without horizontal scroll.

## Build plan (PR-sized)

1. **Request, state and API.** Events, state machine, routes, config, caps, public-404 test, no execution yet.
2. **Runner skeleton with a stub agent.** Host and model selection, mirror, clone, state files, cleanup and crash reconciliation, tested with fakes. No Pi yet.
3. **Sandboxed Pi in herdr.** Workspace and tab, `herdr agent start` under the sandbox with the egress proxy, the prompt, watching, deadline and cancel, association and registration. Needs spikes 1 and 2.
4. **Result gate.** Bounds, flags, branch push, draft PR, summary.
5. **UI.** Button, status card, Terminal commands, cross-links.
6. **Hardening.** The CI-secrets mitigation (spike 5) and a dry run on a throwaway repository before turning `enabled` on.

## Spikes (guesses to settle before the matching step)

1. **herdr detection under bubblewrap.** Does `herdr agent start --kind pi` detect Pi when it runs inside `collab-sandbox.sh` in the pane, so it registers and reports state? If not, start the pane's process differently or have the runner register the session explicitly.
2. **Transcript visibility.** Give the box a writable, per-run bind for Pi's session directory that the host feed plugin can read, so transcript usage and the conversation view work. Without it the Agents page shows the agent but little of what it did.
3. **`herdr worktree` with a mirror.** Whether `herdr worktree create` can make the work copy from the mirror and whether the box can be given the git directory read-only. If so, switch from a standalone clone to a worktree, as the request literally says.
4. **Forge credentials.** Which credential the runner uses per forge (the repository host is GitHub for most projects; the credential must be a file on the host, scoped to push `herdr-ci-fix/*` branches and open PRs where the forge allows it).
5. **Woodpecker and secrets on PR pipelines.** Confirm what `herdr-ci-fix/*` branches and draft PRs can access, and set the rule (approval required or no secrets) before this is enabled. Release blocker.
6. **Interactive Pi startup.** Pi's prompt must be passed as an argument and its first model call hung on 2026-10-22 (memory `collab-host-fleet-runner`); confirm the interactive path works with the chosen models.

## Decisions needed from the operator

Defaults stand until you answer; each can change on request.

1. **Who pushes.** Default: the runner pushes the branch and opens a draft PR after the agent quits. Alternative: the runner leaves the commits on the host and shows a "Push and open PR" button, so a human decides before anything leaves the machine. Safer, one more click.
2. **Draft PR or ready.** Default: draft.
3. **Hosts.** Default: configured per YAML, off until you list them. Say which hosts may run fix agents (bender and beast are also benchmark hosts).
4. **Network inside the box.** Default: the LLM gateway only, plus per-project allowlists. Alternative: open network like the collaboration agents have, which makes tests that fetch packages work but removes the main containment.
5. **Interactive or headless.** Default: interactive Pi in a herdr tab you can attach to. Alternative: headless one-shot, simpler and cheaper to run, but you cannot step in.
6. **Auto-trigger.** Default: click only. Say if failures on certain branches (for example `main`) should start a fix automatically; I would want the attention inbox and caps in place first.

## Built notes (2026-10-24)

Build-plan steps 1–5 are merged; step 6 (hardening against live hosts) is **not done**. The feature ships disabled (`ci_fix.enabled: false`) and nothing has been deployed or run against a real host, forge, herdr server or Woodpecker. Everything below that says "unverified" is an assumption in code, covered only by tests against fakes (plus local bubblewrap box tests).

| Step | PR | What |
|---|---|---|
| 1 | #227 | Request, state fold, API, `ci_fix` YAML block (default disabled; routes 404 when off) |
| 2 | #228 | Host runner: claim, mirror clone, pinned commit, prompt, per-host cap, stub agent |
| 3 | #230 | Sandbox launcher (herdr pane + bubblewrap + egress proxy), watch loop, cleanup |
| 4 | #231 | Result gate: checks, push of the fix branch only, draft PR, crash-repeat safety |
| 5 | this PR | Dashboard: "Fix in sandbox" button, status card, Terminal commands, Cancel, PR link |

### Decisions applied (the six defaults)

1. The runner pushes the branch and opens the PR after the agent quits.
2. The PR is a draft.
3. Hosts come from YAML (`ci_fix.hosts`) and are off until listed.
4. The sandbox reaches the LLM gateway plus per-project allowlisted hosts only.
5. Interactive Pi in a herdr tab.
6. Click-only trigger.

### Deviations from the design

- **One runner per host instead of round robin.** Each host's systemd timer claims one request at a time; a per-host cap and the global cap replace "round robin among hosts".
- **Runner settings are served by the control plane** (`GET /api/v1/ci/fix/config`), so there is one YAML block and no runner-side config file.
- **Claim `defer {kind: no_model}`** keeps a request queued with a visible reason and fails it after `queue_timeout_minutes` (default 30).
- **Idle detection uses a screen fingerprint**, not herdr's idle state (unreliable; see memory `herdr-agent-status-idle-is-unreliable`). An unreadable screen is never counted as idle.
- **The pane runs the boxed command via `pane run`**, not `herdr agent start --kind pi`, which runs the canonical `pi` directly and cannot be put in the box.
- **The `ci-fix` session role is not set**; only the inspector association (branch, worktree label, PR) is written. Registration relies on herdr's detection with an explicit `pane report-agent` fallback.
- **PR base** is the failing branch when it is known and exists on the remote, otherwise the remote's default branch. Only GitHub pull requests are supported.
- **Systemd units are templates** in `deploy/control-plane/` and are not installed.
- **UI:** the card polls with the page's existing 5 s refresh (no new timer); the button is hidden when the routes answer 404.

### Spikes 1–6: status

Run 2026-10-24 on bender, **locally and read-only**: a throwaway herdr server (`herdr --session cifixspike`, started in its own tmux), the real `HerdrPiLauncher`, the real `collab-sandbox.sh`/egress proxy, real Pi against the real gateway (`llm.metabolomics.us`), `ci.metabolomics.us` through GET calls only, and `gh api` reads. Nothing was pushed, no pull request or Woodpecker setting was touched, and no host other than this one was used.

1. **herdr detection under bubblewrap — REFUTED, fallback verified.** Pi inside the box was never detected by herdr: `herdr agent get` returned an error for 45 s while Pi was running and answering. `pane report-agent --source ci-fix-runner --agent pi --state working --agent-session-id <fix>` registers it (`agent get` then shows `pi`, `working`). Consequences, fixed: (a) herdr keeps the last reported state, so the runner now reports `working` while the screen moves and `idle` when it stands still (`HerdrPiLauncher.show_state`; herdr shows idle as `done`), and only when it did the registering; (b) every herdr call now addresses the configured server with `--session <name>` (it did not: a non-default `--herdr-session` created the workspace on the default server). The 30 s wait for detection stays only as a cheap check for other herdr versions.
2. **Transcript visibility — REFUTED as designed, partly fixed.** The feed plugin only reads Pi sessions under `~/.pi/agent/sessions` (`session-model.js`, `session-transcript.js`), while the boxed Pi writes to its private harness directory (confirmed: `harness/pi/sessions/--<cwd>--/<ts>_<uuid>.jsonl`), so the live conversation is **not** visible in Agents and used to be deleted with the box. Fixed: `stop` copies the finished session file(s) (regular files only, no links, 20 MB cap, mode 0600) to `~/.pi/agent/sessions/--ci-fix-<id>--/` before the harness is deleted (verified: the file lands there). Still unverified: that the feed plugin then lists that file in Agents history. Live visibility would need the box to write into the host sessions directory, which would expose every other Pi session to the box, so it is not done.
3. **`herdr worktree` with a mirror — VERIFIED: possible, but not usable in the box; standalone clone stays.** `herdr worktree create --cwd <bare mirror> --branch herdr-ci-fix/x --base main --path <dir>` works against a bare mirror. But a worktree's `.git` points into the mirror, and a commit inside a box with the mirror mounted read-only fails (`mirror.git/worktrees/<n>/index.lock: Read-only file system`). A writable mirror would let the agent write objects and refs into the copy shared by every fix of that repository, so the design keeps the standalone clone.
4. **Forge credentials — PARTLY CHECKED, still unverified for the runner.** Read-only: the operator's own GitHub login has scopes `admin:org, gist, repo` (far more than needed; do not use it) and `push`/`admin` on this repository, which has an active branch ruleset "Independent AI review" and no classic branch protection. The runner needs a dedicated token that can push `herdr-ci-fix/*` branches and open pull requests only (fine-grained: Contents write, Pull requests write, Metadata read, no Workflows) in a mode-0600 file. No push or pull-request call was made, so whether that token passes the ruleset is unverified.
5. **Woodpecker secrets on PR pipelines — CONFIRMED, release blocker stands.** Read-only API facts (secret values are never returned by the API): 118 active repositories; all have `allow_pr: true` and `require_approval: forks`, so a pull request from a **branch of the same repository**, which is what a fix branch is, runs without approval. Repository secrets are exposed to `pull_request` events on three of them: `metabolomics-us/go-modules` (AWS access key pair), `metabolomics-us/lc-binbase-scheduler` (BinView export JSON, test AWS key pair) and `metabolomics-us/wcmc` (AWS key pair and `wcmc_prod_ro_password`). The global secret `ghcr_read_token` is also exposed to `pull_request`. `berlinguyinca/herdr-engineering` is not active in Woodpecker. Code fix: `control_plane.ci_fix.repositories` (`deny` always wins; a non-empty `allow` limits fixes to those repositories), refused with a clear reason at request time. **Still to do by the operator before enabling:** list those three repositories under `repositories.deny` (or set their `require_approval` to all events / remove `pull_request` from those secrets in Woodpecker) and decide whether `ghcr_read_token` on pull requests is acceptable.
6. **Interactive Pi startup and first model call — VERIFIED, with one fix.** Interactive Pi, started as `pi --model <m> --no-extensions --no-skills --no-prompt-templates --no-context-files -- "$(cat PROMPT.md)"` inside the box with only the egress proxy, answered through the gateway in seconds (`metabolomics/qwen3.8-flash_next-modality-text-quant-q4_k_xl`; the prompt-as-argument hang of 2026-10-22 did not reproduce; the proxy allows only the gateway). The box could not start `pi` at all on this host: `~/.local/bin/pi` is the session-auto-resume wrapper that `exec`s node on files under `~/.local/share` and `~/.npm-global`, which the box did not see ("Cannot find module"). Fixed in `collab-sandbox.sh`: for a harness that is a shell script under `$HOME`, the directories of the absolute `$HOME` paths on its `exec` line are exposed read-only (nothing else; a file not named there stays invisible, tested). Not tested: the other configured models, and a long run.

Session id format `herdr:<host>:<server>:<urlencoded pane>` — **VERIFIED against the feed plugin source** (`snapshot.js:149`, every component `encodeURIComponent`ed; the server name is the `herdr session list` name, `default` for the main one) and against the runner's output (`herdr:spiketest:cifixspike:w6%3Ap1`). Not exercised: the live feed plugin picking the registered pane up.

What the operator still has to do (nothing below was done): create the dedicated forge token and its 0600 file; settle the Woodpecker rule above; install the runner timer on the chosen hosts; do the dry run on a throwaway repository with the real token (push, draft pull request, CI on the fix branch); then set `ci_fix.enabled`.

### Try run attempt (2026-10-24, after the control plane deploy of `ce713629bb7b`)

- **Deploy:** the first two attempts ended in "failed" and "failed to roll back" only because the control plane takes several minutes to load its event history and `deploy.sh` waits 60 s by default; the old image then came up healthy by itself. A third run with `HERDR_CP_HEALTH_TRIES=150 HERDR_CP_HEALTH_INTERVAL=5` succeeded. Raise those for any later deploy.
- **Found by the attempt, fixed in #250:** the lab gateways answer `GET /v1/models` with 401, so the runner could never choose a model. It now falls back to the ids Pi's own `models.json` lists for that gateway.
- **Spike 4 (forge credentials) — STILL UNVERIFIED.** Not run, deliberately: the request needs the repository to exist in Woodpecker with a real failed pipeline (`ci_run_context` asks the CI provider), and registering a throwaway repository there exposes agent-written branches to the global Woodpecker secret `ghcr_read_token`, which is enabled for `push` and `pull_request` events with no image restriction. A fix branch (or the agent editing a root `.woodpecker.yaml`, which the default `paths.flag` does not cover) could read it. The operator has to restrict that secret (events and images) or accept the exposure before any real run. `ci_fix` was not enabled.
