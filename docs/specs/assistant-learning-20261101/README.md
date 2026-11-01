# Learning from corrections (step 2: measure only)

Specialists are optional and chosen on demand (see assistant-management-20261031). Learning starts by measuring when you
correct an agent, with no behavior change.

`herdr-eng corrections enable|disable|status|scan|list|forget` (`herdr_engineering/corrections.py`):

* **Opt-in.** `scan` refuses until `enable`. `disable` stops further recording and keeps what exists; `forget --session <id>` or
  `forget --all` deletes it (naming nothing is refused).
* **Pull, not push.** `scan` reads one session afterwards through `session_history` (read-only, redacted, clipped) for
  Claude Code, Codex, Pi and OpenCode. Nothing runs in an agent's request path, so there is no added latency.
* **Four deterministic kinds:** `negation` (a leading "no / wrong / instead / don't / actually / undo / revert / I said" after an
  agent turn), `reask` (a near-duplicate of a recent ask: word overlap >= 0.6, at least 4 words, within 12 events),
  `interrupt`, `rejected_tool` (a failed tool result saying the user declined). No model reads your chats.
* **Small.** A record is harness, session id, a 160-character excerpt of what you said and 120 characters of what you were
  answering. Never a transcript, never hidden reasoning. Files are `0600` in a `0700` directory under
  `$HERDR_ENGINEERING_STATE/corrections/`. Re-scanning a session records nothing new (stable ids).

Not built: scanning on a schedule or from a hook, proposals from repeated corrections (step 3), and the before/after
correction-rate measurement (step 4). The patterns are English-only and will miss and over-match; the point of step 2 is to
read the log and find out how much.
