# Session summary (`/summary`)

One canonical, read-only skill (`skills/summary/SKILL.md`) that writes an evidence-based
recovery report for the **current** agent session: objective, timeline, verified vs
reported work, test results, repository state, next actions, and an explicit
*Evidence coverage* verdict (`complete` / `partial` / `limited`).

There is no service and no new store. Session history is read from each agent's own
records through `herdr-eng summary history`, which reuses Herdr pane metadata
(`herdr pane get`) and the existing journal projection.

## Install and upgrade

`scripts/install.sh` installs it by default (step 3), and the fleet converges it on every
host (`ansible/tasks/summary-skill.yml`, included from `site.yml` and `fleet-sync.yml`).
By hand:

```bash
herdr-eng summary install                    # global (default)
herdr-eng summary install --scope project    # current repository only
herdr-eng summary install --dry-run          # show what would change
```

Rerun it to upgrade. Global layout:

| What | Where |
| --- | --- |
| Canonical skill (one copy) | `~/.local/share/herdr-engineering/skills/summary/` |
| Claude Code, Codex, Pi, OpenCode skill dirs | symlinks to the canonical copy in `~/.claude/skills`, `~/.codex/skills`, `~/.pi/agent/skills`, `~/.config/opencode/skills` |
| Pi `/summary` | `~/.pi/agent/prompts/summary.md` |
| OpenCode `/summary` | `~/.config/opencode/command[s]/summary.md` (reuses an existing `command/` dir) |
| Ownership record | `…/skills/.summary-manifest.json` (sha256 of every file written) |

Project scope uses `.agents/skills/summary` (read by Codex, Pi, OpenCode), a relative
`.claude/skills/summary` link, `.pi/prompts/summary.md` and `.opencode/commands/summary.md`.

### Never overwrites your files

A file is rewritten on upgrade only while it still matches what the installer last wrote.
A customized prompt, a pre-existing `summary` skill, or a different symlink is reported
as `preserved`, left byte-identical, and warned about on stderr. `herdr-eng summary
uninstall` removes only unmodified files and links that point at our copy.

## Invoke

| Agent | Invocation |
| --- | --- |
| Pi | `/summary` (run `/reload` after installing) |
| Claude Code | `/summary` |
| OpenCode | `/summary` |
| Codex | `$summary`, or `/skills` and pick `summary` |

Codex gets no command file: a bare custom `/summary` is not a documented Codex feature,
and the installer will not shadow a built-in. If a future Codex adds custom slash
commands, add an adapter and a test then.

## Verify the installation

```bash
herdr-eng summary verify          # exit 0 only if skill, all four links and both commands are present
herdr-eng summary verify --json
```

`ok` = matches the canonical source; `stale` = older managed copy (rerun install);
`preserved` = your customized file; `missing` = not installed. Then, in each agent, run
`/summary` (Codex: `$summary`) in a throwaway session; it must open with
`# Session summary` and end with *Evidence coverage*.

Tests: `pytest tests/test_session_summary.py tests/test_session_history.py`.

## What it reads, and its limits

`herdr-eng summary history` picks the session by (1) explicit `--session`, (2) Herdr
pane metadata (`HERDR_PANE_ID` → `herdr pane get`), (3) only then the newest record in
the working directory, labelled `newest-in-cwd (heuristic)` and capped at `limited`.
It reads Claude Code and Pi/Codex JSONL and OpenCode's SQLite database (opened
`mode=ro`), redacts through `control_plane.redact`, and never writes anything.

- **Compaction:** Claude Code, Pi and OpenCode keep pre-compaction turns in their
  records, so they are included and marked. If a record holds only the compaction
  summary, coverage is `partial`.
- **Child agents:** Claude Code (`isSidechain`, `subagents/`) and OpenCode (child
  sessions) expose child records. Pi and Codex expose dispatches and final messages
  only; the report says so.
- Long sessions are elided in the middle (first 60 + last events); the omitted count is
  reported.
- Codex failure detection from tool output is a text heuristic; treat `ok` there as
  weaker evidence than for the other three.
- A report is a snapshot: repository state is read now, not as of the session start.
- `git` is run with `--no-optional-locks`, so even `git status` does not touch the index.
- **Codex:** Herdr reports no conversation for idle Codex panes, so the helper identifies one from Codex's own process log (pid + thread id, newer than the process, primary conversations only; a tie is reported, not guessed). The method shows as `herdr-pane (codex process log)`.
- Herdr's own `journal summary` projection is included only if a journal file already
  exists.

## Full report in the dashboard

The agent page has a **Full report** button next to the model-written **Quick summary** button. It runs the same `summary` skill the terminal uses and shows the result in the
summary card (a bounded, scrollable block) instead of the terminal. In the fullscreen
workspace the summary and report sit in their own column beside the conversation and
activity panes, so the panes and composer keep the whole viewport.

It is off by default. Turn it on in the central fleet configuration (YAML only):

```yaml
summary_report:
  enabled: true
  harness: claude          # the summarizer; only claude is accepted until codex has run live
  timeout_seconds: 300     # 10-900
  max_concurrent: 2        # 1-4 open reports across the fleet
```

Where it runs. The control plane is a container with neither the agents' session records nor a
summarizer CLI, so it only queues the request (`POST /api/v1/sessions/{id}/summary/report`, operator,
same-origin JSON; a durable `SummaryReportRequested` event). The session service on the host that owns the
agent (`herdr-cp-sessions`, the same process that publishes snapshots) polls
`POST /api/v1/host/summary-reports/claim` every 5 s with its host token (`host.json` `host_id`), runs
`herdr-eng summary report` locally and posts the result to `.../result`. One job at a time per host. A
request nobody claims within 90 s fails as `host_unreachable` (usually: that host's service has not been
updated); a claimed job that never reports fails as `timeout`. Both leave the button usable for a retry.

How it stays read-only:

- It never resumes or prompts the target session. The host reads the session record
  (`herdr-eng summary history`) and runs fixed-argv, read-only `git` in the recorded worktree itself
  (`--no-optional-locks`, `core.fsmonitor=false`, `--no-ext-diff`), then starts a fresh `claude -p` in an
  empty scratch directory with **no tools**, `--no-session-persistence`, `--strict-mcp-config` and no Herdr
  pane environment. The evidence is passed as quoted data; nothing in a transcript is executed.
- The report must start with `# Session summary` and contain **Evidence coverage**, otherwise it is
  discarded as `malformed_report`. The host validates it and the control plane validates and redacts it
  again before storing it (capped at 60,000 characters) as a session event (`SummaryReportCompleted`), so
  it survives a restart and is returned as `full_report` by `GET /api/v1/sessions/{id}/summary`.
- One open report per session; a second click returns the running job.
- The host endpoints use the ingest role, like snapshots: a host token is not bound to one host, so a
  token holder could claim another host's job, the same trust the ingest path already has. A result is
  only accepted for the request it names and the host that request was queued for.

Limits (v1):

- The agent must have identified its conversation (`agent_session`). An idle Codex pane that has not run
  a turn since it started has none yet, and the button says so.
- The host needs `herdr-eng` and `claude` on the session service's PATH (`~/.local/bin` is included) with
  working Claude credentials for the user that owns the session records.
- Roll the plugin (`deploy/deploy-fleet-agents.sh`) to every host that should be able to run reports.
- `herdr-eng summary report --agent A --session ID [--harness claude]` prints the same
  result as JSON for use from a terminal.

### Reading the report

The report block opens with a strip: overall status, an **Evidence** badge (`complete`, `partial` or
`limited`, copied from the report's own coverage verdict) and the **Resume point**. The numbered sections
below are collapsible (outstanding work and next actions start open). When the conversation has moved on
since the report was made, a **stale** badge shows how many newer messages exist and offers **Regenerate**.
While a report runs the status line shows elapsed time. **Copy as markdown** copies the stored report;
**Send to agent** posts the resume point and next actions to the agent as a message (confirmed first, and
disabled once the agent has ended). Timestamps in the report that match a recorded turn jump to that turn
(best effort: a timestamp with no turn within a day stays plain text). A failed or empty quick summary is
reduced to one muted line once a full report exists. In the fullscreen workspace, token-usage rows are hidden
from Activity unless **Show usage rows** is ticked, and **Hide activity** gives the conversation the full width.

### Running `/summary` in a terminal

In a Herdr pane, `/summary` (or `$summary` in Codex) no longer prints the whole report into the scrollback.
It publishes the report and prints one line:

- `herdr-eng summary publish --agent A --session ID` reads the report on stdin, validates and redacts it, and
  writes `~/.cache/herdr-engineering/summary/<agent>-<session>.md` (mode 0600).
- It posts the report to the control plane (`POST /api/v1/host/summary-reports/publish`, ingest role), where it
  becomes the session's `full_report` with `source: "terminal"`. The dashboard names the agent by its recorded
  identity; zero or several matching dashboard sessions are refused (`no_session`, `ambiguous_session`) rather
  than guessed, and a running dashboard job is not cancelled.
- It opens, or reuses, a read-only `less -R` side pane to the right of the agent (`--no-focus`), remembering
  the pane in a sidecar file. `--no-pane` and `--no-dashboard` turn either half off.
- If publishing fails, the skill prints the complete report so nothing is lost. Target mode and prepared
  evidence runs never publish.

### Work spread over several worktrees

A session that moved into a different git worktree records every distinct `cwd` (latest last). The report's
git facts come from the latest directory that is a repository; earlier distinct worktrees are listed as
`other_worktrees` and reported as such, never merged into one state.
