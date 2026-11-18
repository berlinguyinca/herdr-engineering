# Automatic handoff and the session side pane

Date: 2026-11-18. Status: draft for approval.

## Why

A Herdr session that fills its context should move to a fresh session on its own. Today it does not: every
handoff step depends on the agent choosing to run `/handoff`, and the agent regularly stops and asks the user to do
it instead. That defeats the purpose, most of all in long unattended runs (overnight work with several
subagents in flight).

The user also wants to *see* the work move: a live side pane next to the session showing the current summary, the
chain of handoffs, and the CI state of the work in that chain.

Reliability is the core requirement. A failed handoff should happen well under 1% of the time, and when one does,
nothing may be lost.

## Goals

1. **Harness-driven trigger.** Herdr, not the agent, decides when to hand off, based on the session's measured
   context use. No user prompt, no agent discretion about *whether*.
2. **Drain, then hand off.** At the limit the session stops taking new work, waits for its running subagents to
   finish and collects their reports, then writes its briefing.
3. **Forced briefing with fallback.** Herdr orders the session to write the briefing now. If none arrives within a
   timeout, a second model writes it from the transcript, git state and open PRs.
4. **Takeover below, clean exit.** The successor opens in a new pane directly *below* the source pane, same tab,
   same harness. Only after Herdr itself observes the successor running and having read the briefing does the source
   session get a clean exit request, and then its pane closes.
5. **Linked chain.** Each handoff record names its predecessor and successor, in one shared store, so the chain can
   be drawn and counted across repos.
6. **Side pane.** One collapsible pane on the right of the tab with three tabs, all read-only and live:
   - **Summary**: goal, done, now, next. Refreshed at every handoff from the briefing.
   - **Handoffs**: the chain as a small vertical tree. The live session is highlighted. Each node shows session
     number, a few-word label, duration, and the live session's context gauge. Totals: handoffs, sessions, total
     time. Failed handoffs show red with the reason. A draining session shows e.g. "waiting for 3 subagents".
   - **CI**: one card per PR/branch the chain opened or pushed: title, overall state (running/failing/ready), one
     chip per check (Woodpecker steps, GitHub Actions, GitGuardian, review bots), which session pushed it and
     when, and a one-line failure reason when readable. A check that never started (e.g. GitHub billing refusal)
     is grey "not started", not red. Merged PRs collapse into one line. The tab label shows a dot when something
     is running or failing. Informational only: no click actions at this stage.
7. **One policy.** This replaces both existing context policies: autospec's tmux context monitor (50% compact /
   80% rollover) and herdr-engineering's 50% `/handoff` reminder hooks, including the PreCompact/SessionStart
   "run /handoff yourself" text added in PR #882.

## Non-goals

- Clicking a CI card to act on it (fixing, rerunning). Later, maybe.
- Handing running subagents over to the successor. They finish in the source session.
- Reading Woodpecker logs with a token. Never.

## Anti-goals (failure even if it "works")

- The session asks the user to do or approve the handoff.
- The source session or its pane closes before a successor is proven live.
- Running subagents are killed by a handoff, or their results vanish.
- Uncommitted work is lost.
- Two policies fight over the same session (one compacts while the other hands off).

## The handoff, step by step

1. **Watch.** A Herdr-side controller reads each managed session's context use from the harness's own records
   (for Claude Code: the transcript JSONL usage of the last assistant turn, which every hook already gets as
   `transcript_path`; better still the statusline JSON, which states the context window — the builder should pick
   whichever states the window size rather than inferring 200k vs 1M). It does not depend on prompt hooks being
   installed.
2. **Limit reached → drain.** At the *drain limit* (one user setting, default 80% — my call), the controller types a
   fixed order into the session: start no new work; wait for running subagents and take their reports; commit and
   push the work on its feature branch (never main, never herdr-engineering main); then write the briefing with
   `herdr-handoff create`; then stop. The order leaves no room to ask the user. While draining, the Handoffs node
   says how many subagents it is waiting for.
3. **Headroom.** Subagent reports consume context while draining. The drain limit sits below a *hard ceiling*
   (default: drain limit + 12 points, before the harness's own auto-compaction — my call). If the ceiling or a
   drain timeout (default 30 min — my call) is hit first, the handoff proceeds anyway and the briefing lists which
   subagents were still running and where their worktrees/branches are.
4. **Briefing fallback.** If no briefing record appears within the briefing timeout (default 5 min — my call), the
   controller has a second model (headless harness call) write one from the transcript, `git status`/log of the
   worktree, and `gh pr list` for the branch. The fallback briefing is marked as such in the record and in the
   Handoffs node.
5. **Uncommitted work.** The existing dirty-worktree guard must never turn into a question to the user. If the
   session did not commit, the controller (not the agent) makes a WIP commit on the feature branch and pushes it
   where pushing is allowed; where it is not allowed (herdr-engineering without a push approval), it records the
   unpushed commits in the briefing and proceeds — the worktree stays on disk. My call.
6. **Successor.** The controller runs the existing takeover with placement *below* the source pane (`herdr pane
   split --direction down`), same tab, same harness, with the briefing. The source pane stays visible (dimmed in
   the tree) until the successor is confirmed.
7. **Proof of life.** As today: Herdr observes the successor's agent state and the handoff id in its output (not
   the successor's own say-so). Docs and code must agree on this (today they don't).
8. **Clean exit.** The controller asks the source harness to exit cleanly (harness-appropriate exit command), waits
   for the process to end, then closes the pane. Today the pane is just closed and the process dies with it.
9. **Record.** The handoff record gets `predecessor_id`/`successor_id` (and the source/child pane and session ids,
   which are null in real records today). The side pane updates.
10. **Failure.** If any step fails, the source session keeps running and keeps its pane; nothing is closed. The
    controller retries idempotently (takeover is already idempotent per handoff id) a bounded number of times.
11. **Last resort: compaction.** If retries are exhausted and the session reaches the harness's own compaction, the
    controller lets compaction happen. A briefing is generated anyway (the session's own if it wrote one, otherwise
    the second-model fallback), and after compaction the controller tells the compacted session to read that
    briefing, so it resumes from it rather than from the lossy compacted summary. The controller then tries the
    handoff again at the next limit.

Every event is recorded and shown in the Handoffs tree, colour-coded (the user's scheme):
- **green**: handed off successfully, work continues in the successor;
- **orange**: handoff failed, continued in compaction (node links the generated briefing);
- **red**: stuck (no successor and no compaction yet), with the reason.

## Constraints

- Herdr itself (v0.9.1) is a closed binary. Build on its CLI (`pane split/run/close/wait-output`, `agent wait`,
  `pane resize`, `pane zoom`) and its plugin panes (`herdr-plugin.toml` `[[panes]]`). Herdr has no tabs inside a
  pane and no collapse: the three tabs live inside one TUI, and "collapse" is resizing/hiding that pane.
- Reuse what exists: `herdr-handoff` create/takeover/confirm (installed 0.4.0; the dirty-guard work on branch
  `feat/takeover-dirty-source-guard` is not on master), `summary_report.py` + `pane_viewer.py`, the agent-ops plugin
  TUI pattern, `herdr_engineering/ci.py`, control-plane `pr_trail.py` and `handoffs.py` chain join.
- The subagent registry (`handoff_subagent_registry.py`) exists but is not wired into takeover and its hooks are not
  installed. Waiting for subagents depends on it (or an equivalent), so it must be wired and installed.
- Handoff store: today relative to cwd (`.herdr/handoffs`), fragmented per repo. Move to one shared store (or index)
  so a chain can cross repos. Existing per-repo records keep working.
- **All four harnesses, every session — non-negotiable.** Claude Code, Codex, Pi and OpenCode, and every session of
  them running in Herdr, with no opt-in. A per-session off switch may exist for deliberate exceptions, but the
  default is on. Each harness needs an adapter for: reading its context use and window, typing the drain order,
  detecting idle/prompt state, and a clean exit. `docs/operations/handoff-auto-triggers.md` marks the Codex,
  OpenCode and Pi parts "Not verified"; verifying them is part of the work, not a reason to ship without them. Where
  a harness does not state its context use, the adapter measures it from that harness's own session records.
- Repo rules apply: branch → PR → review → Woodpecker green; no mocks in tests; nothing under /tmp; never push
  herdr-engineering main; never use CI tokens.

## Decided with the user

- Trigger is automatic and harness-driven; the agent never asks.
- The old session exits and its pane closes, only after the successor is proven live.
- The successor pane opens *below* the source.
- Side pane on the right, collapsible, three tabs: Summary, Handoffs (tree + totals), CI (informational only).
- On failure: old session stays, node red with reason, nothing closed. Must work >99% of the time.
- Running subagents: wait for them to finish before handing off.
- Scope: Claude Code, Codex, Pi and OpenCode; every session; non-negotiable.
- Repeated failure: allow compaction as last resort, still generate a briefing and point the compacted session at
  it; tree colours green (handed off), orange (failed, continued in compaction), red (stuck).
- Briefing: forced order to the session, second-model fallback.

## My calls (check these)

- Drain limit default 80%; hard ceiling +12 points; drain timeout 30 min; briefing timeout 5 min.
- Controller-made WIP commit when the session leaves work uncommitted.
- One shared handoff store.
- Retire autospec's context monitor and the herdr-engineering reminder hooks rather than keep them alongside.
- Merged PRs collapse into one line in the CI tab; GitHub "not started" checks show grey.
- Limits are percentages of the session's actual context window (read, not inferred, where the harness states it).
- **Typing safety.** The controller only types the drain order when the session is idle at its input with an empty
  input line (Herdr `agent wait --until idle` plus a visible-screen check). If the user is typing, the session is
  mid-turn, or a permission prompt is showing, it waits and re-checks; it never merges into a user draft or answers
  a prompt. Mid-turn waiting counts against the hard ceiling like any other wait.
- **Unknown subagent count.** When the subagent registry has no data for a session, the drain order asks the
  session to list its running background agents and to wait for them; the controller then waits until the session
  reports drained or the drain timeout. The registry hooks are installed as part of rollout so new sessions always
  have data.
- **Side pane per tab.** One side pane per tab, created automatically by the controller with the first managed
  session in the tab. It shows the chain of the focused session pane; with one session per tab (the normal case)
  that is simply the tab's chain.
- **Repos and order.** Several PRs: (0) per-harness adapters (Claude, Codex, Pi, OpenCode) with their verification; (1) herdr-handoff: merge `feat/takeover-dirty-source-guard`, add `down`
  placement, predecessor/successor ids, filled session/pane ids, clean source exit; (2) herdr-engineering: wire and
  install the subagent registry; (3) herdr-engineering: the controller (trigger, drain, forced briefing, fallback,
  takeover, retries); (4) herdr-engineering: the side-pane TUI plugin; (5) retirement: disable autospec's context
  monitor for Herdr-managed sessions (its own setting, not deleting autospec) and remove herdr-engineering's
  reminder hooks — shipped in the same release as (3) so two policies never run at once.

## Mockups

`2026-11-18-auto-handoff-mockups/handoff-layout.html` (the three moments of a handoff, Summary and Handoffs tabs)
and `2026-11-18-auto-handoff-mockups/ci-tab.html` (the CI tab). Open them in a browser; they are layout and
content guides, not pixel specs.

## Left to the builder

- Where the controller runs (Herdr plugin process, a small daemon, or a `herdr-eng` service) and how it discovers
  managed sessions.
- Exact transport of the drain order (typed text vs. a harness-native mechanism), within the 900-byte typed-command
  limit.
- The TUI layout and refresh mechanism of the side pane, following the mockups (Summary / Handoffs / CI).
- CI data plumbing (`gh pr checks`, `herdr-eng ci pipelines`, control-plane PR trail) and polling cadence.
- Migration of existing per-repo handoff records into the shared store.
- Test strategy across the four layers, with real Herdr panes and real git repos.
