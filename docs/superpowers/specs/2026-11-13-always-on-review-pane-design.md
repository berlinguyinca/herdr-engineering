# Always-on review pane design

Status: proposal for operator review, 2026-11-13. Everything under "Design" is a **proposal** unless stated as a decision.

## Goal

A dedicated, always-on cross-system review process in herdr-engineering, visible as a herdr pane. It reviews every open PR head once, cheaply where possible, and posts the status the ruleset requires.

Decisions already taken by the operator:

- Ruleset 25691548 requires the commit status `Opus agent review`, posted per exact head SHA (PR #788 documents the current manual flow: a Claude session runs an Opus subagent and posts it). `Independent AI review` is advisory.
- Interim host (operator, 2026-11-13): **beast**, until a dedicated node is up. The design stays host-agnostic so the move is a re-install, not a rewrite: a `systemd --user` unit or a herdr pane. beast is a production LLM host, so the reviewer must stay light (concurrency 1, no GPU use, no change to its serving units), and the operator installs it (nothing here deploys itself). It needs a Claude Code login and `gh` auth on beast, and a clone it can create read-only worktrees from.
- The operator wants to save money by using open-weight models where possible.

## Motivation: measured evidence

On the pre-fix diffs of #733 (7 issues found by Opus) and #782 (6 issues found by Opus), three open-weight candidates (deepseek-v4-flash, qwen3.8-27b-q8_k_xl, qwen3.8-flash_next-q4_k_xl) found **0 of the 13 clearly**, with 2 weak partial hits. They also reported 3-4 other real-looking issues on #782 (a dedup collision on `event_id`, an untested bridge path). Whether those were outside Opus's list of 13 has **not been confirmed** and the scoring needs to be redone against Opus's list before this evidence is relied on. They also raised **4 false blocking findings**, for example calling `table[:-1] + '_archive"'` at `herdr_engineering/control_plane/bridge.py:402` an invalid identifier (on the pre-fix #782 diff). Latencies were 15-240 s. deepseek has one slot and returns 429 when called in parallel.

Conclusion: open-weight models are useful as a cheap first pass but are not trustworthy as the gate.

## Design (proposals)

### Reuse `ai-review.py poll`

Keep the structure of `deploy/ci-review/ai-review.py poll`: list open PRs, review once per head SHA, idempotent (a terminal status for the head means done), retry a stale pending status after `pending_stale_minutes`, the **poster decides the state** (never the model), and drop findings that cite files not in the PR. Today `ai-review.py` defaults `poll.skip_drafts` to `false` (`ai-review.py:98`, used at `:1149`), so drafts are skipped only when `poll.skip_drafts: true` is set. The proposal sets it `true` in the config of the review unit. New code goes in a sibling module or a `--tier` option so the existing `Independent AI review` path keeps working.

### Two tiers

| | Tier 1 | Tier 2 |
| --- | --- | --- |
| Reviewer | open-weight model via the pi gateway | local headless Claude Opus: `claude -p --model opus` |
| Runs on | every push (every new head SHA) | only a head that is otherwise mergeable: every check that ran on the head is green (contexts `ci/woodpecker/pr/{artifact-handoff,structural-gates,unit-gates,ui-gates}`, `verify`, `GitGuardian Security Checks`; `ui-gates` only runs for certain paths, so a missing one is not a wait) |
| Effect | **advisory only**; never posts `Opus agent review`. Posts under a different context, e.g. `Open-weight pre-review`, and optionally one marked PR comment | posts `Opus agent review` success or failure |
| Tools | none (diff as data, as today) | read-only: Read, Grep, Glob and an allowlisted `git diff`, enforced with `--allowedTools` / `--disallowedTools`. No WebFetch, WebSearch or write tools. `claude -p` still needs API reach and its login on the host |

Tier 2 runs in a fresh detached worktree of the exact head SHA, created per review and removed after. Running it only on green heads spends Opus tokens once per mergeable head rather than once per push.

### Verdict schema

Strict JSON, anything else is a failed review (status stays pending): `{"head_sha": str, "verdict": "approve"|"block", "findings": [{"file": str, "line": int|null, "severity": "blocking"|"note", "category": "code"|"security"|"tests"|"docs"|"performance", "why": str}], "checked": [str]}`. The poster verifies `head_sha` equals the pinned SHA, caps list and text sizes, drops findings on files outside the PR, and sets success only when no blocking finding remains.

### Prompt-injection posture

PR title, body, diff, commit messages and file contents are untrusted data, fenced with random delimiters and never placed in instructions. The Tier 2 process is restricted to Read, Grep, Glob and allowlisted `git diff` (no WebFetch, WebSearch or write tools). It still needs API reach and the host's Claude login, so it is not network- or secret-free, but an injection can only skew the verdict text. The script, not the model, decides the posted state, and re-reads the PR head before posting; a head that moved posts nothing.

### Limits and failure behaviour

- Concurrency 1 per host; one review at a time.
- Per-review wall-clock cap and token cap (values are an open question below).
- Cooldown for a PR whose reviews keep failing (for example 3 failures, then wait before retrying).
- When a review cannot run (gateway down, timeout, malformed answer, claude not logged in), the status stays **pending**. Never post success on failure to run.
- Re-review on every push: a new head SHA has no status, so poll picks it up; the old head's status is irrelevant.

### Measuring a candidate before trusting it

Build a replay set: past PRs (start with #733 and #782 pre-fix diffs, add more) with Opus findings as ground truth. For each candidate model, run the review and score each ground-truth issue as found, missed, or partial, and count false positives (blocking findings with no real defect). A model may gate only if its false-blocker rate is about 0 on the replay set and recall is acceptable to the operator. Until then Tier 1 stays advisory. Re-score when the model, quantization or prompt changes.

### Independence trade-off

Opus reviewing Claude-authored PRs drops the different-family rule that `Independent AI review` enforces. Proposal: for risky paths (deploy, migrations, security), add Codex as a second reviewer whose verdict must also be success; the path list lives in YAML config. This is a proposal and costs extra, so it is limited to those paths.

### The pane

A herdr pane tails the reviewer log (`journalctl --user -fu herdr-review` (`herdr-review` is a proposed unit name) or the state dir log), showing per-PR lines: head, tier, state, duration, tokens. The unit also writes one JSON line per review to its state dir for later cost accounting. Configuration is YAML only (no environment variables), like `ai-review.yaml`.

## Failure modes

- Opus quota exhausted or login expired: status stays pending, pane shows the error, operator is not paged by a false green.
- Open-weight gateway down or 429: Tier 1 skips; Tier 2 is unaffected.
- Host down: PRs wait pending; a manual session can still post the status as today (PR #788).
- False blocker from Opus: operator re-runs the review by hand or overrides through the existing manual path; findings are in a PR comment.

## Rollback

Rollback is to stop and uninstall the review unit and fall back to the manual post (PR #788). This spec changes no ruleset. Any ruleset change is a separate operator step with its own description and rollback. Note that the raw `gh api repos/berlinguyinca/herdr-engineering/rulesets/25691548` GET output cannot be PUT back unchanged, because it has read-only fields (`id`, `_links`, `created_at`, `updated_at`, `source`, and similar); strip them before any restore. `Independent AI review` stays available throughout.

## Open questions

1. Which dedicated node replaces beast, and when.
2. Opus token budget per review and per day, and the wall-clock cap.
3. Does Tier 1 post a PR comment, or only log? (Comments are visible but noisy.)
4. Which paths count as risky for the Codex second reviewer.
5. Which models and how large a replay set before Tier 1 is allowed to influence anything.
