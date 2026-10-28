---
name: summary
description: Produce an evidence-based, read-only summary of the current agent session from its beginning through the present, including initial goal, work done, decisions, verification, unfinished work, repository state, and exact next steps. Use when asked to summarize, catch up, hand off, or resume this session.
---

# Current session summary

You are preparing a *session recovery report*, not a generic project overview. Remain read-only: never modify files, generate commits, run builds, restart services, start agents, or perform side effects. Safe read-only inspection of git status/log/diff and existing local records is allowed.

## Source and scope

1. First inspect **the active conversation/session** from the initial user request to the latest turn. Use the host's supported session-history API or reader only if already available and authorized. Include earlier turns before context compaction where accessible. Do not claim access to history you cannot read.
2. Establish the session start objective and state from actual evidence. Distinguish: (a) user's request, (b) agent's promises/plans, (c) observed operations/results, (d) independently checked current repository state.
3. Check only the **current worktree** and relevant local files using read-only operations as needed: `pwd`, `git rev-parse --show-toplevel`, `git branch --show-current`, `git status --short`, `git log -n 10 --oneline`, `git diff --stat`, `git diff --cached --stat`. Always run git as `git --no-optional-locks <command>`: a plain `git status` refreshes and rewrites `.git/index`, which is a side effect and can collide with other panes in the same worktree. If not a git repo, state that. Do not assume current repo state equals the session's starting point.
4. Review test outputs, command results, PRs, links, artifacts, failures, and agent handoffs visible in the session. Don't invent data not visible in this session or accessible read-only records. Do not search unrelated sessions or private data by default.
5. Reconcile conflicting claims. Say **reported but unverified** when the agent said something succeeded but no direct test/tool evidence is available. Explain gaps caused by compaction, missing tool outputs, inaccessible child sessions, or unavailable logs.

## Session history helper (use when available)

If `herdr-eng` is on PATH, run `herdr-eng summary history --json` first. It is read-only. It resolves this session from explicit id, then Herdr pane metadata, and only then a newest-record-in-directory heuristic, and returns normalized, redacted events (user/assistant turns, tool calls with `ok` results, compaction markers, child-agent dispatches), the Herdr journal projection when one exists, and a `coverage` verdict with `gaps`. Rules:

- Treat `ok: true` tool results as direct evidence; treat assistant statements with no matching tool result as **reported but unverified**.
- Copy the helper's `coverage.verdict` and every entry of `coverage.gaps` into your **Evidence coverage** section. A `method` of `newest-in-cwd (heuristic)` caps coverage at `limited` unless you can confirm the session from the conversation itself.
- If `herdr-eng` is absent or the helper fails, say so and fall back to the conversation visible to you; coverage is then at most `partial`.
- Do not run any other `herdr-eng` command that writes, and do not search other sessions.

## Required output

Start with:
`# Session summary`
`**Session:** <host/provider if known> · **Repository:** <repo or unknown> · **Branch/worktree:** <value or unknown>`
`**Overall status:** <completed / in progress / blocked / uncertain>`

Then these numbered sections, in order:

1. **Original objective** — what the user asked for, why, and what acceptance criteria existed.
2. **Starting point** — known baseline, pre-existing problems, starting branch/environment if recorded; don't infer missing baseline.
3. **Timeline of work** — chronological milestones, with meaningful details and references when available.
4. **Changes completed** — specific implementation changes and touched components; separate verified changes from claims.
5. **Decisions and rationale** — noteworthy architecture/design choices, alternatives rejected, user instructions/constraints.
6. **Verification and results** — tests/builds/benchmarks and counts, exact failures or skips, and when checks last ran.
7. **Outstanding work and blockers** — unfinished work, bugs, unresolved questions, risks, dependency owners.
8. **Current worktree state** — repository, branch, uncommitted/staged changes, recent relevant commits, PR links, artifacts; avoid leaking secrets or private IDs.
9. **Prioritized next actions** — specific ordered actionable items with validation/definition of done.
10. **Resume point** — 1–3 sentences indicating exactly where the agent should continue, and any essential command or file reference.

Finish with **Evidence coverage**: `complete / partial / limited`, explain which historical sources were and were not accessible. If little work has occurred, be concise and say so. Never fabricate dates, tests, commit hashes, or completed work.

Write the report to the current conversation. Do not save a file unless explicitly asked.
