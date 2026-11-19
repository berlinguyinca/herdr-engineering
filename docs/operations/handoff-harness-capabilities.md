# Handoff harness capabilities (measured)

Where each agent harness records its context size and its context window, measured on 2026-11-18 from real
session records with `scripts/measure_harness_windows.py` (read-only; it prints field names, model ids and
numbers, no message text). The adapters in `herdr_engineering/handoff_adapters/` implement what is
recorded here. A reading above 100% means the resolved window is wrong, so the script flags every recent
session in that state.

Window rule, shared by all adapters: the window the harness itself recorded (`window_source='recorded'`),
else `MODEL_WINDOWS` (`'model_table'`; a `-YYYYMMDD` dated id falls back to its undated entry), else 128,000
(`'fallback'`). `read_context` returns `None` only when no usage has been recorded yet, never because a window is
missing. The fallback errs towards handing off early only for a model whose real window is at least 128,000; a model with a smaller real window and no recorded limit would be read too low, and a handoff would come late.

## Claude Code

| Item | Measured |
| --- | --- |
| Record location | `~/.claude/projects/<cwd with every non-alphanumeric as "-">/<session id>.jsonl` |
| Context size | latest main-chain assistant entry (`isSidechain` false, model not `<synthetic>`): `message.usage.input_tokens + cache_read_input_tokens + cache_creation_input_tokens`; output tokens are not included (same sum as `handoff_context_hook`) |
| Window source | Transcripts record none. Claude Code's own model catalogue, `~/.claude/cache/model-catalog/<org>-<hash>-cc.json`, field `catalog.config.models[].runtime.max_input_tokens` (one file per account; the newest `fetchedAt` that lists the model wins). `window_source='recorded'`; a dated and an undated spelling of an id match each other (the catalogue lists `claude-haiku-4-5-20251001` dated and everything else undated). A model the catalogue does not list falls to `MODEL_WINDOWS`, then 128,000. When both list the model and disagree, see "Catalogue against table" below |
| Corroboration (not used) | The statusline input JSON carries `context_window.context_window_size`; a cached copy written by the oh-my-claudecode HUD (`~/.claude/hud/cache/stdin.<session>.json`) shows 1,000,000 for `claude-sonnet-5-5`, which agrees with the catalogue. No `statusLine` is configured in `~/.claude/settings.json`, and the cache belongs to a third-party plugin and goes stale, so it is not read |
| Checked, no window | `~/.claude.json` has no per-model window (`tengu_hawthorn_window`, 200000, is a feature flag with no model attached). No `[1m]` suffix appears in transcript model ids or in `requestedModel`; it is a model-setting suffix (the binary says to "append [1m] to the model name for 1M" and to set `CLAUDE_CODE_MAX_CONTEXT_TOKENS` for a custom id), and the transcript records the bare id |
| Compaction marker | a `system` record with `subtype: "compact_boundary"` (265 in the last 30 days); a usage entry before the latest one is stale, so there is no reading until the next reply |
| Session id in Herdr | `herdr agent get <pane>`, `agent_session.kind == "id"`; the transcript is `<id>.jsonl` in the project directory, matched by id even when the pane cwd differs from the session directory. Without an id there is no session (`locate` returns None): the one transcript a project directory may hold can belong to an older session, and a wrong reading would trigger a false handoff |
| Unconfirmed | (1) Whether `max_input_tokens` equals the effective window for a session whose model setting has no `[1m]`. For the 5.x models in use here it does: `claude-sonnet-5-5` sessions reached 966,904 tokens and `claude-opus-5-5` 568,085, so their window is above 200,000. For models not seen recently the catalogue (1,000,000 for `claude-opus-4-8`) and the table (200,000, from the autospec monitor) disagree, the adapter uses the smaller window unless the session's used tokens already exceed it, then the larger; nothing measures which is right. (2) Nothing measures which window a session really has for the three conflicting models below |

Model ids seen in the last 30 days and how each resolves:

| Model id | Window | `window_source` | Where from |
| --- | --- | --- | --- |
| `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-opus-5`, `claude-fable-5-1`, `claude-fable-5-2` | 1,000,000 | `recorded` (catalogue present), else `model_table` | the catalogue's `max_input_tokens`; the same values are in `MODEL_WINDOWS` for a machine without the catalogue |
| `claude-opus-4-5-20251101`, `claude-sonnet-4-5-20250929` | 200,000 | `model_table` | not in the catalogue; the dated id resolves to `claude-opus-4-5` / `claude-sonnet-4-5` (200,000, autospec context monitor table); the binary says every dated id of a version resolves to the undated id |

Catalogue against table. Both are consulted; if only one lists the model it is used (`recorded` or `model_table`); if they agree it is `recorded`; if they differ the smaller wins, with the `window_source` of whichever supplied it, unless the session's own used tokens already exceed the smaller window, which proves it wrong for that session, and the larger is used. Why: a window that is too large makes a nearly full session look empty so a handoff fires late or never, while one that is too small only hands off early. The models where the two disagree on this machine are `claude-opus-4-7`, `claude-opus-4-8` and `claude-sonnet-4-6` (catalogue 1,000,000, table 200,000), so those read as 200,000 `model_table` until usage passes 200,000. The catalogue also lists `claude-fable-5`, `claude-haiku-5-5`, `claude-mythos-5-1`, `claude-mythos-5-2`, `claude-opus-4-6` and `claude-sonnet-5` (1,000,000), which the table lacks, so they are `recorded` where the catalogue is present and fall back to 128,000 without it.

Window unknown, falls back to 128,000: none of the ids seen. Before this task `claude-opus-5-5` was not in the table, so a real
session at about 247,000 tokens read as 193% of a 128,000 fallback.

## Codex

| Item | Measured |
| --- | --- |
| Record location | `~/.codex/sessions/<Y>/<M>/<D>/rollout-<timestamp>-<session id>.jsonl` |
| Context size | `event_msg` records with `payload.type == "token_count"`: `payload.info.last_token_usage.total_tokens` is the current context. It fell from 785,180 to 35,545 across a compaction while `total_token_usage` (cumulative) kept growing, so the cumulative figure is not used |
| Window source | `payload.info.model_context_window` on the same event (828,400 for `gpt-6.1-sol` and `gpt-6-astra`). `window_source='recorded'`. `~/.codex/config.toml` has `model_context_window = 1000000`, which disagrees with what the events record; the recorded figure wins. When an event records none, the model of the preceding `turn_context` goes through `MODEL_WINDOWS` |
| Compaction marker | a top-level record with `type: "compacted"` (29 in the last 30 days); a usage event before it is stale, so there is no reading until the next `token_count` (in 16 of 16 real compactions one followed, with a lower value) |
| Session id in Herdr | `agent_session.kind == "id"`; the rollout file name ends in `-<id>.jsonl`, matched by id |
| Prompt, exit, compact | Measured, see "Idle detection and exit" below. `agent_session` is absent for a fresh pane (see below), so `locate` finds no record until Herdr reports one |

Model ids seen: `gpt-6.1-sol`, `gpt-6-astra`, both `recorded`. Window unknown: none.

## Pi

| Item | Measured |
| --- | --- |
| Record location | `~/.pi/agent/sessions/<cwd slug>/<timestamp>_<session uuid>.jsonl` |
| Context size | latest assistant `message.usage.totalTokens` (input + output + cache read + cache write of that reply, the context the next request starts from). Aborted and errored replies record all zeros and are skipped |
| Window source | Session records hold none. Pi's `~/.pi/agent/models.json`, `providers.<provider>.models[].contextWindow`, matched on the reply's `message.provider` and `message.model`. `window_source='recorded'` means Pi's own configuration for that model, not a figure written with the usage entry. `~/.pi/agent/models-store.json` (Pi's built-in catalogue for `anthropic` and `openai-codex`) lists windows too but none of the sessions seen used those providers, so it is not read |
| Compaction marker | a record with `type: "compaction"`; after it there is no reading until the next reply |
| Session id in Herdr | `agent_session.kind == "path"` (a record file); the uuid is the file name's suffix. The session is selected by that id, in the pane cwd's directory first, then in any directory |
| Unconfirmed | Window of the model ids `models.json` does not list (below). Prompt, exit and compact are measured, see "Idle detection and exit" below |

Window unknown, falls back to 128,000 (these ids are not in `models.json`, and no other source lists them):
`qwen3.8-27b-modality-vision-quant-q8_k_xl`, `qwen3.8-27b-text`, `qwen3.8-flash-text`,
`qwen3.8-flash_next-modality-text-quant-w3a8`, and, seen once each with too little usage to flag,
`qwen3.8-27b-modality-vision-ctx-c120k-quant-q4_k_m`, `deepseek_v4-flash-modality-text-quant-mxfp4`,
`deepseek_v4.1-flash-modality-text-quant-mxfp4`, `qwen3.8-flash_next-modality-text-quant-q4_k_xl`, `qwen3.7-max-next`.
Four sessions on the first three read above 100% of 128,000 (up to 231,644 tokens), which proves that 128,000 is too small for
them. The base ids listed in `models.json` have 131,072 or 262,144, and a `-text` or `-vision` id listed there always equals its base
id, but a window for an unlisted id is not inferred from that: one id carries a `ctx-c120k` cap in its name. Listing these ids
in `models.json` is the fix; until then they read high and a handoff comes early, not late.

## OpenCode

| Item | Measured |
| --- | --- |
| Record location | SQLite `opencode.db` under `$XDG_DATA_HOME` (default `~/.local/share/opencode/`), table `message`, column `data` (JSON), keyed by `session_id` |
| Context size | latest assistant message `data.tokens.total` (input + output + reasoning + cache read + cache write). A reply that never completed records zeros and is skipped |
| Window source | The database holds none (no column of `session` or `session_context_epoch`, and no field of `message.data`, names a window). OpenCode's config `~/.config/opencode/opencode.json`, `provider.<provider>.models.<model>.limit.context`, then its cached catalogue `~/.cache/opencode/models.json`, `<provider>.models.<model>.limit.context`; both `window_source='recorded'`. Else `MODEL_WINDOWS`, else 128,000 |
| Compaction marker | an assistant message with `summary: true` and `mode: "compaction"`; after it there is no reading until the next reply |
| Session id in Herdr | `agent_session.kind == "id"`; matched against `session.id` on its own, without comparing the session `directory` with the pane cwd. Without an id there is no session (`locate` returns None; OpenCode reports no id until the first turn): the one session a cwd may hold can be an older one, and a wrong reading would trigger a false handoff |
| Prompt, exit, compact | Prompt, exit and compact measured, see "Idle detection and exit" below. `agent_session` (kind `id`, a `ses_...` value) appears only after the first prompt, so `locate` finds no record before it |

Model ids seen: `qwen3.8-27b`, `qwen3.8-27b-vision` (provider `example`), both `recorded`. Window unknown: none.

## Idle detection and exit

Measured on 2026-11-18 in real Herdr 0.9.1 panes, each opened with `herdr pane split --no-focus` and closed afterwards,
by `tests/e2e/test_handoff_adapters_live.py` (`pytest tests/e2e/test_handoff_adapters_live.py -m herdr_live -v -s`).
Every test types without submitting, except `test_one_turn_then_idle_detection_and_session_kind`, which submits exactly
one trivial prompt ("Reply with the single word ok.") per harness in the pane the test opened and then checks the screen
a handoff would really face (the first measurement was a fresh pane with no session). The screen the adapters read is what
`HerdrPaneClient.read_visible` returns, `--format ansi`, which carries colour codes in every row;
`herdr_engineering/handoff_adapters/screen.py` reads it. Screens captured from the same panes, redacted, are the fixtures
`tests/fixtures/handoff_adapters/<harness>/screen_{empty,typed,after_turn_empty,after_turn_typed}.ansi`, and
`opencode/screen_update_dialog.ansi`.

| Harness | Version | Empty prompt as measured | Exit (measured) | Seconds to process end | Compact | `agent_session` before a prompt | `agent_session` after a turn |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude Code | 2.1.306 | row `❯` + no-break space, then a faint (SGR 2) suggestion such as `Try "fix lint errors"`, between two rules | `/exit` + `Enter`; measured | 0.4 to 1.2 | `/compact` + `Enter`; slash menu lists "/compact  Free up context by summarizing the conversation so far", not submitted | kind `id` | kind `id` |
| Codex | 0.169.0 | row `›` (bold), a braille animation cell, a faint `Ask Codex to do anything`, more braille cells; typed text replaces the faint part | `/exit` + `Enter`; measured | 0.5 to 0.7 | `/compact` + `Enter`; slash menu lists "/compact  summarize conversation to prevent hitting the context limit", not submitted | none reported | none reported (see below) |
| Pi | 1.3.0 | a blank row between two rows of `─`, with the footer below; typed text sits on the middle row | `/quit` + `Enter`; measured | 0.2 to 0.7 | `/compact` + `Enter`; slash menu lists "compact  Manually compact the session context" (shown without the slash), not submitted | kind `path` | kind `path` |
| OpenCode | 1.18.23 | prompt box of `┃` rows ending in a `┃  Build · <model>` row and a `╹▀▀▀` edge; a fresh session shows `Ask anything... "<example>"` in it (the example changes), after a turn the empty box is bare `┃` rows | `/exit` + `Enter`; measured | 0.4 to 0.6 | `/compact` + `Enter`; measured: a session-less pane does NOT list it, after a turn the slash menu lists "/compact  Compact session" (typed, not run) | none reported | kind `id`, value `ses_...` |

How each was used, and what was found:

- **Marker alone is not enough for Claude Code and Codex.** An empty composer shows a suggestion after the marker, so a bare
  `❯` or `›` row never occurs in a live pane. The suggestion is faint (SGR 2); typed text is not. `typed_text` in `screen.py`
  drops faint runs and Codex's braille animation cells and calls the prompt empty when nothing else follows the marker.
  Plain text (no colour codes) cannot tell a suggestion from typing, so there any text after the marker counts as typed; a caller
  must pass the ANSI screen. The suggestion text itself is not matched, because Claude Code's changes between runs.
- **A multi-line draft can leave the marker row bare.** In a real Claude Code, Codex, Pi and OpenCode pane, a newline typed
  into an empty composer (Ctrl+J; Alt+Enter in OpenCode) and then text gives a bare marker row with the text on the next row.
  Claude Code and Codex used to read that as empty, so a controller would have typed into the user's draft.
  `composer_is_empty` in `screen.py` now requires every row from the marker row to the composer's closing row to be blank:
  the closing rule for Claude Code, the status row for Codex (found as the text block at the bottom of the screen, one spacer
  row below the composer; with no spacer row the adapter says not ready). Pi (the editor is one blank row between two rules,
  any text breaks that shape) and OpenCode (every `┃` input row must be bare or the placeholder) were already safe;
  `screen_multiline.ansi` of each harness is the real capture that proves it.
- **Pi** needed no logic change: stripping the colour codes leaves the blank-row-between-two-rules editor the adapter already matched.
- **After a turn the screen was re-measured** (Claude Code, Codex, Pi, OpenCode, one real turn each). Claude Code, Codex and
  Pi keep their layout: Claude Code's empty composer is a bare `❯` (no suggestion) two rows above the bottom, Codex's is
  `› Ask Codex to do anything` with three rows below, Pi's editor is unchanged with two footer rows. So
  the Claude Code and Codex row limits and `_LINES_BELOW_EDITOR` hold. **OpenCode changed**: the placeholder is gone once a session has
  a turn, so a placeholder-based check would never see an empty composer in a session that has had a turn. The adapter
  now finds the `╹▀` bottom edge of the prompt box (at most 4 non-blank rows below it: 3 in a fresh session, 1 after a
  turn), requires the row above it to be the agent/model row, and calls the box empty when every `┃` input row above that
  is bare or starts with the placeholder. `agent_status` is `idle` while OpenCode is still "Loading plugins...", so
  status alone does not mean ready.
- **Modal dialogs in front of the prompt.** Codex ("Update available", `enter continue · esc skip`) and OpenCode ("Update
  Available", `Skip  Confirm`) put an update dialog over the prompt at start; Escape dismisses both without changing
  configuration, and the live test does that. OpenCode's box and placeholder stay visible behind its dialog, so the adapter
  returns False while a `Skip  Confirm` row is on screen (real capture, fixture `opencode/screen_update_dialog.ansi`).
  **Codex's dialog is not covered by a test**: its real capture was lost when the same-named file was overwritten, and the
  dialog did not come back, because one earlier run of the measurement sent Enter into it and Codex recorded that update as
  dismissed (`dismissed_version` 0.170.0 in `~/.codex/version.json`); it will return with the next Codex release. The screen
  as read before that (full-screen, a `› 1. Update now` selection row, `2. Skip`, `3. Skip until next version`,
  `enter continue · esc skip`) puts non-blank text after the `›` marker, so the existing rule should answer False, but that is
  unverified. Treat Codex's update dialog as unhandled until a capture exists. Claude Code and Codex stop at a trust
  dialog for a folder they have not seen; saving the trust decision changes user config, so the live test skips instead
  (`HERDR_LIVE_CWD` points it at a trusted folder).
- **Pause between text and Enter.** Codex ignored an `Enter` sent in the same instant as `/exit` (the text stayed in the
  composer, 0 of 3 runs) and obeyed it after 0.1 s (3 of 3 runs at 0.1, 0.3 and 0.6 s). `base.TYPE_TO_SUBMIT_DELAY_S`
  (0.3 s) is the pause a sender must put between typing and the submit keys; the other three harnesses were not tried without it and get it too. The pause is data on `TypedInput.submit_delay_s` and
  `ExitPlan.submit_delay_s` (default `TYPE_TO_SUBMIT_DELAY_S`), and `handoff_adapters.send.send_typed(client, pane_id, typed)`
  types the text, waits it, then sends the submit keys; every sender must use that helper, and the live tests do. The slash
  menu does not need an extra Enter: with it open, one `Enter` runs `/exit` and `/quit` (`/compact` was only typed, never run).
- **Process end** is when every foreground process of the pane is a shell, from `herdr pane process-info`. The harnesses end in
  at most 1.1 s including the 0.3 s pause; `ExitPlan.process_end_timeout_s` stays 15 s as margin. Claude Code's process name
  is its version (`2.1.306`), so do not match it by name.
- **Codex `/exit` disconnects, it does not stop work.** Its screen reads "Disconnected from this task. Any running work
  continues." (Codex 0.169.0 runs turns in a local background server). That is harmless when the session is idle, which
  the handoff requires before exiting, but `/exit` on a working Codex would leave the turn running.
- **`herdr agent get` `agent_session`**, measured before a prompt and after one turn. Claude: `{kind: "id"}`, the conversation
  id, from the start and after. Pi: `{kind: "path"}`, the record file, from the start and after. OpenCode: none before a
  prompt, `{kind: "id", value: "ses_..."}` after the first one; `OpenCodeAdapter.locate_session` matches that value against
  `session.id`, so `locate` works for it once a turn exists. **Codex: none, before a prompt and after a turn**, although its
  hook integration (v8) is "current" and the turn wrote a rollout file; so `agent_session` is never a rollout id for Codex.
  **How `CodexAdapter.locate` finds the rollout anyway** (measured on Codex 0.169.0, 2026-11-18).
  `herdr pane process-info` gives one foreground process named `codex` with its pid, cwd and an argv that is only the binary
  path; no start time. The start is taken as now minus `ps -o etime=` (run with `LC_ALL=C`; `lstart` is localised and
  reordered by `ps` in other locales, elapsed time has no locale or zone). The session id is in neither the argv nor the
  environment. `lsof -p <pid>` shows sqlite state and log files and **no rollout**: Codex runs turns in a shared background
  server (`app-server`) and that process, not the pane's, holds a rollout open. So there is no direct signal. The rule: Herdr's
  id when it reports one; otherwise a rollout the pane's process holds open (none in practice, and `lsof` is optional);
  otherwise the single rollout under `~/.codex/sessions` that (1) records the process's cwd (from `process-info`, the caller's
  cwd if that has none; compared resolved), (2) was written (mtime) since the process started, and (3) is interactive:
  `session_meta.originator` is `codex-tui`. Measured `session_meta`: a TUI session has `originator: codex-tui`,
  `source: vscode`; a `codex exec` run has `originator: codex_exec`, `source: exec`. A resumed session (`codex resume <id>`)
  appends to its **old** rollout, with no new `session_meta` and the old start time; so the session's own start is not
  compared, only the file's write time. Zero or several candidates give `None`; the newest file is never chosen.
  **Limits:** two interactive Codex sessions in the same cwd both written since the pane's process started (a second pane in
  that cwd, or `/new` in the same process) give no reading, and so does a pane whose process has not written yet. The durable
  fix is Herdr's Codex integration reporting `agent_session`. Why it does not now: Herdr's Codex hook
  (`~/.codex/herdr-agent-state.sh`, a `SessionStart` hook whose payload carries `session_id`) reports through the
  `HERDR_PANE_ID` of its environment, and the shared `app-server` that runs Codex turns inherited the environment of the pane
  that first started it (a pane that no longer exists), not of the pane in use; Claude Code and Pi run their hooks in the
  pane's own process. The live test (`test_one_turn_then_idle_detection_and_session_kind[codex]`) asserts the located rollout
  holds the test's own prompt and gives a reading; it fails if another Codex session is written in the same cwd meanwhile. The
  Codex update dialog stays unhandled (see above). Codex's own `/status` shows a session id of the same shape as
  the one in a rollout file name, and `codex resume <id>` on exit prints one; it was not compared with a rollout.

Evidence for the compact command is the harness's own slash menu after the command was typed (never submitted), for Claude
Code, Codex and Pi in a fresh pane and for OpenCode after a turn; `/compact` was never run in a live session. Exit was run.

## Rules the adapters implement beyond the original plan

1. After a compaction there is no reading until the next reply (all four adapters: Claude `compact_boundary`, Codex `compacted`, Pi `compaction`, OpenCode `summary`). The usage entry before the marker is the
   size of the context that was just discarded; reading it would trigger a handoff for a context that is already small.
2. A session is selected by its id even when the pane's cwd differs from the session's directory (all four adapters; Pi looks in
   the cwd's directory first and then anywhere, OpenCode never compares `directory`). The id comes from Herdr for that pane, which
   is what makes this safe; with two sessions in one cwd, selection is by id, never "newest file".

## Script output

`python3 scripts/measure_harness_windows.py`, run on 2026-11-18 (home paths shortened to `~`, provider names shown as `*` and no session counts, by the script):

```
== claude (last 30 days) ==
where a window or model limit is recorded:
  transcript ~/.claude/projects/*/<session>.jsonl: none found
  model catalogue ~/.claude/cache/model-catalog/*-cc.json: catalog.config.models[].runtime.max_input_tokens [200000, 1000000]
  global config ~/.claude.json: cachedGrowthBookFeatures.*.windowDays [30]; cachedGrowthBookFeatures.tengu_hawthorn_window [200000]
  statusLine in ~/.claude/settings.json: not configured
  statusline input cache ~/.claude/hud/cache/stdin.*.json: *.context_window_size [1000000]
models seen in recent sessions (window and window_source as the adapter resolves them):
  claude-fable-5-2  window=1000000  window_source=recorded
  claude-opus-4-5-20251101  window=200000  window_source=model_table
  claude-opus-5  window=1000000  window_source=recorded
  claude-opus-5-5  window=1000000  window_source=recorded
  claude-sonnet-4-5-20250929  window=200000  window_source=model_table
  claude-sonnet-5-5  window=1000000  window_source=recorded
sessions above 100% of their window:
  none

== codex (last 30 days) ==
where a window or model limit is recorded:
  rollout ~/.codex/sessions/Y/M/D/rollout-*.jsonl: payload.*.window_id; payload.info.model_context_window [828400]; payload.model_context_window [828400]; payload.rate_limits.primary.window_minutes [10080]; payload.state.context_window_guidance
  config ~/.codex/config.toml: model_context_window [1000000]
models seen in recent sessions (window and window_source as the adapter resolves them):
  gpt-6-astra  window=828400  window_source=recorded
  gpt-6.1-sol  window=828400  window_source=recorded
sessions above 100% of their window:
  none

== pi (last 30 days) ==
where a window or model limit is recorded:
  session ~/.pi/agent/sessions/*/<ts>_<uuid>.jsonl: none found
  models ~/.pi/agent/models.json: providers.*.models[].contextWindow [131072, 262144]
  model store ~/.pi/agent/models-store.json: [].models[].contextWindow [128000, 200000, 272000, 1000000]
models seen in recent sessions (window and window_source as the adapter resolves them):
  */deepseek_v4  window=262144  window_source=recorded
  */qwen3.8-27b  window=262144  window_source=recorded
  */qwen3.8-27b-modality-vision-quant-q8_k_xl  window=128000  window_source=fallback
  */qwen3.8-27b-text  window=128000  window_source=fallback
  */qwen3.8-flash  window=262144  window_source=recorded
  */qwen3.8-flash-text  window=128000  window_source=fallback
  */qwen3.8-flash-vision  window=262144  window_source=recorded
  */qwen3.8-flash_next  window=131072  window_source=recorded
  */qwen3.8-flash_next-modality-text-quant-w3a8  window=128000  window_source=fallback
  */qwen3.8-flash_next-text  window=131072  window_source=recorded
  */qwen3.8-vision  window=262144  window_source=recorded
sessions above 100% of their window:
  OVER 100%: */qwen3.8-27b-text used=231644 window=128000 window_source=fallback
  OVER 100%: */qwen3.8-27b-modality-vision-quant-q8_k_xl used=154018 window=128000 window_source=fallback
  OVER 100%: */qwen3.8-flash-text used=193802 window=128000 window_source=fallback

== opencode (last 30 days) ==
where a window or model limit is recorded:
  database ~/.local/share/opencode/opencode.db, message.data: none found
  database columns of session, session_context_epoch: none found
  config ~/.config/opencode/opencode.json: provider.*.models.*.limit.context [65536, 131072]
  model catalogue ~/.cache/opencode/models.json: [].models.*.limit.context 204 values, 0..99999999
models seen in recent sessions (window and window_source as the adapter resolves them):
  */qwen3.8-27b  window=131072  window_source=recorded
  */qwen3.8-27b-vision  window=65536  window_source=recorded
sessions above 100% of their window:
  none
```

## Headless fallback briefing writer

When a drained session writes no briefing, `handoff_controller/briefing.py` asks the same harness for one in a headless
call: stdin prompt, a fresh empty working directory under `<store>/fallback-work/`, every `HERDR_*` variable and the
harness's session-identity variables removed. A harness is enabled only if its own `--help` shows how to turn every tool
off, or (Codex) a sandbox that stops writes in an empty directory. Only `--help` was run; no real model call was made, so the last column is what is still unverified.

| Harness | Enabled | Flags | Unverified without a real call |
| --- | --- | --- | --- |
| claude | yes | `-p --output-format text --no-session-persistence --setting-sources "" --strict-mcp-config --disable-slash-commands --tools ""` | empty values for `--setting-sources` and `--tools` accepted, and user/project hooks dropped; prompt read from stdin |
| pi | yes | `-p --no-session --no-tools --no-extensions --no-mcp --no-skills --no-prompt-templates --no-context-files` | prompt read from stdin with no message argument; provider login still usable with `--no-extensions` |
| codex | yes | `exec --sandbox read-only --skip-git-repo-check --ephemeral -` (`codex exec --help`: prompt read from stdin when absent or `-`; `--ephemeral` writes no session files) | tools stay on but the read-only sandbox stops writes and the work directory is empty; prompt read from stdin; login still usable with `--ephemeral`; no network restriction was seen in `--help` |
| opencode | no | `opencode run` offers `--agent` and `--auto` but no command-line way to deny tools | not enabled |

Stripped from the environment: `HERDR_*`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SESSION_ID`,
`CLAUDE_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_SANDBOX`, `CODEX_SANDBOX_NETWORK_DISABLED`, `CODEX_MANAGED_BY_NPM`,
`PI_CODING_AGENT`, `PI_SESSION_ID`, `OPENCODE`, `OPENCODE_PID`, `OPENCODE_SESSION_ID` (the first four groups are the
variables `handoff_harness.py` reads to detect an ancestor harness). Kept: auth and config such as `ANTHROPIC_*`,
`OPENAI_*`, `HOME`, `PATH`.
