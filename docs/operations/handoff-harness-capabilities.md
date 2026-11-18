# Handoff harness capabilities (measured)

Where each agent harness records its context size and its context window, measured on 2026-11-18 from real
session records with `scripts/measure_harness_windows.py` (read-only; it prints field names, model ids and
numbers, no message text). The adapters in `herdr_engineering/handoff_adapters/` implement what is
recorded here. A reading above 100% means the resolved window is wrong, so the script flags every recent
session in that state.

Window rule, shared by all adapters: the window the harness itself recorded (`window_source='recorded'`),
else `MODEL_WINDOWS` (`'model_table'`; a `-YYYYMMDD` dated id falls back to its undated entry), else 128,000
(`'fallback'`). `read_context` returns `None` only when no usage has been recorded yet, never because a window is
missing. A fallback window errs towards handing off early, never late, for a model whose real window is larger.

## Claude Code

| Item | Measured |
| --- | --- |
| Record location | `~/.claude/projects/<cwd with every non-alphanumeric as "-">/<session id>.jsonl` |
| Context size | latest main-chain assistant entry (`isSidechain` false, model not `<synthetic>`): `message.usage.input_tokens + cache_read_input_tokens + cache_creation_input_tokens`; output tokens are not included (same sum as `handoff_context_hook`) |
| Window source | Transcripts record none. Claude Code's own model catalogue, `~/.claude/cache/model-catalog/<org>-<hash>-cc.json`, field `catalog.config.models[].runtime.max_input_tokens` (one file per account; the newest `fetchedAt` that lists the model wins). `window_source='recorded'`. A model the catalogue does not list falls to `MODEL_WINDOWS`, then 128,000 |
| Corroboration (not used) | The statusline input JSON carries `context_window.context_window_size`; a cached copy written by the oh-my-claudecode HUD (`~/.claude/hud/cache/stdin.<session>.json`) shows 1,000,000 for `claude-sonnet-5-5`, which agrees with the catalogue. No `statusLine` is configured in `~/.claude/settings.json`, and the cache belongs to a third-party plugin and goes stale, so it is not read |
| Checked, no window | `~/.claude.json` has no per-model window (`tengu_hawthorn_window`, 200000, is a feature flag with no model attached). No `[1m]` suffix appears in transcript model ids or in `requestedModel`; it is a model-setting suffix (the binary says to "append [1m] to the model name for 1M" and to set `CLAUDE_CODE_MAX_CONTEXT_TOKENS` for a custom id), and the transcript records the bare id |
| Compaction marker | a `system` record with `subtype: "compact_boundary"` (265 in the last 30 days) |
| Session id in Herdr | `herdr agent get <pane>`, `agent_session.kind == "id"`; the transcript is `<id>.jsonl` in the project directory, matched by id even when the pane cwd differs from the session directory |
| Unconfirmed | (1) Whether `max_input_tokens` equals the effective window for a session whose model setting has no `[1m]`. For the 5.x models in use here it does: `claude-sonnet-5-5` sessions reached 966,904 tokens and `claude-opus-5-5` 568,085, so their window is above 200,000. For models not seen recently the catalogue (1,000,000 for `claude-opus-4-8`) and the table (200,000, from the autospec monitor) disagree; the table entry is kept and nothing measures it. (2) The adapter does not yet treat a `compact_boundary` as "no reading until the next reply", so the reading right after `/compact` is the stale pre-compaction size until the next reply. No task assigned |

Model ids seen in the last 30 days and how each resolves:

| Model id | Window | `window_source` | Where from |
| --- | --- | --- | --- |
| `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-opus-5`, `claude-fable-5-1`, `claude-fable-5-2` | 1,000,000 | `recorded` (catalogue present), else `model_table` | the catalogue's `max_input_tokens`; the same values are in `MODEL_WINDOWS` for a machine without the catalogue |
| `claude-opus-4-5-20251101`, `claude-sonnet-4-5-20250929` | 200,000 | `model_table` | not in the catalogue; the dated id resolves to `claude-opus-4-5` / `claude-sonnet-4-5` (200,000, autospec context monitor table); the binary says every dated id of a version resolves to the undated id |

Window unknown, falls back to 128,000: none of the ids seen. Before this task `claude-opus-5-5` was not in the table, so a real
session at about 247,000 tokens read as 193% of a 128,000 fallback.

## Codex

| Item | Measured |
| --- | --- |
| Record location | `~/.codex/sessions/<Y>/<M>/<D>/rollout-<timestamp>-<session id>.jsonl` |
| Context size | `event_msg` records with `payload.type == "token_count"`: `payload.info.last_token_usage.total_tokens` is the current context. It fell from 785,180 to 35,545 across a compaction while `total_token_usage` (cumulative) kept growing, so the cumulative figure is not used |
| Window source | `payload.info.model_context_window` on the same event (828,400 for `gpt-6.1-sol` and `gpt-6-astra`). `window_source='recorded'`. `~/.codex/config.toml` has `model_context_window = 1000000`, which disagrees with what the events record; the recorded figure wins. When an event records none, the model of the preceding `turn_context` goes through `MODEL_WINDOWS` |
| Compaction marker | a top-level record with `type: "compacted"` (29 in the last 30 days) |
| Session id in Herdr | `agent_session.kind == "id"`; the rollout file name ends in `-<id>.jsonl`, matched by id |
| Unconfirmed | The adapter does not treat `compacted` as "no reading until the next reply". No task assigned. Composer prompt marker and exit command: Task 7 |

Model ids seen: `gpt-6.1-sol`, `gpt-6-astra`, both `recorded`. Window unknown: none.

## Pi

| Item | Measured |
| --- | --- |
| Record location | `~/.pi/agent/sessions/<cwd slug>/<timestamp>_<session uuid>.jsonl` |
| Context size | latest assistant `message.usage.totalTokens` (input + output + cache read + cache write of that reply, the context the next request starts from). Aborted and errored replies record all zeros and are skipped |
| Window source | Session records hold none. Pi's `~/.pi/agent/models.json`, `providers.<provider>.models[].contextWindow`, matched on the reply's `message.provider` and `message.model`. `window_source='recorded'` means Pi's own configuration for that model, not a figure written with the usage entry. `~/.pi/agent/models-store.json` (Pi's built-in catalogue for `anthropic` and `openai-codex`) lists windows too but none of the sessions seen used those providers, so it is not read |
| Compaction marker | a record with `type: "compaction"`; after it there is no reading until the next reply |
| Session id in Herdr | `agent_session.kind == "path"` (a record file); the uuid is the file name's suffix. The session is selected by that id, in the pane cwd's directory first, then in any directory |
| Unconfirmed | Window of the model ids `models.json` does not list (below). The editor-rule prompt marker and exit command: Task 7 |

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
| Session id in Herdr | `agent_session.kind == "id"`; matched against `session.id` on its own, without comparing the session `directory` with the pane cwd. Without an id only an unambiguous top-level, unarchived session in the pane's cwd is used |
| Unconfirmed | Prompt placeholder and exit command: Task 7 |

Model ids seen: `qwen3.8-27b`, `qwen3.8-27b-vision` (provider `metabolomics`), both `recorded`. Window unknown: none.

## Rules the adapters implement beyond the original plan

1. After a compaction there is no reading until the next reply (Pi and OpenCode). The usage entry before the marker is the
   size of the context that was just discarded; reading it would trigger a handoff for a context that is already small.
2. A session is selected by its id even when the pane's cwd differs from the session's directory (all four adapters; Pi looks in
   the cwd's directory first and then anywhere, OpenCode never compares `directory`). The id comes from Herdr for that pane, which
   is what makes this safe; with two sessions in one cwd, selection is by id, never "newest file".

## Script output

`python3 scripts/measure_harness_windows.py`, run on 2026-11-18 (home paths shortened to `~` by the script):

```
== claude (last 30 days) ==
where a window or model limit is recorded:
  transcript ~/.claude/projects/*/<session>.jsonl: none found
  model catalogue ~/.claude/cache/model-catalog/*-cc.json: catalog.config.models[].runtime.max_input_tokens [200000, 1000000]
  global config ~/.claude.json: cachedGrowthBookFeatures.tengu_flint_harbor_prompt.windowDays [30]; cachedGrowthBookFeatures.tengu_hawthorn_window [200000]
  statusLine in ~/.claude/settings.json: not configured
  statusline input cache ~/.claude/hud/cache/stdin.*.json: context_window; context_window.context_window_size [1000000]
models seen in recent sessions (window and window_source as the adapter resolves them):
  claude-fable-5-1  window=1000000  window_source=recorded  sessions=1
  claude-fable-5-2  window=1000000  window_source=recorded  sessions=2
  claude-opus-4-5-20251101  window=200000  window_source=model_table  sessions=3
  claude-opus-5  window=1000000  window_source=recorded  sessions=1
  claude-opus-5-5  window=1000000  window_source=recorded  sessions=133
  claude-sonnet-4-5-20250929  window=200000  window_source=model_table  sessions=1
  claude-sonnet-5-5  window=1000000  window_source=recorded  sessions=54
sessions above 100% of their window:
  none

== codex (last 30 days) ==
where a window or model limit is recorded:
  rollout ~/.codex/sessions/Y/M/D/rollout-*.jsonl: payload.context_window; payload.context_window.window_id; payload.info.model_context_window [828400]; payload.model_context_window [828400]; payload.rate_limits.primary.window_minutes [10080]; payload.state.context_window_guidance
  config ~/.codex/config.toml: model_context_window [1000000]
models seen in recent sessions (window and window_source as the adapter resolves them):
  gpt-6-astra  window=828400  window_source=recorded  sessions=12
  gpt-6.1-sol  window=828400  window_source=recorded  sessions=188
sessions above 100% of their window:
  none

== pi (last 30 days) ==
where a window or model limit is recorded:
  session ~/.pi/agent/sessions/*/<ts>_<uuid>.jsonl: none found
  models ~/.pi/agent/models.json: providers.metabolomics.models[].contextWindow [131072, 262144]
  model store ~/.pi/agent/models-store.json: [].models[].contextWindow [128000, 200000, 272000, 1000000]
models seen in recent sessions (window and window_source as the adapter resolves them):
  metabolomics/deepseek_v4  window=262144  window_source=recorded  sessions=4
  metabolomics/qwen3.8-27b  window=262144  window_source=recorded  sessions=8
  metabolomics/qwen3.8-27b-modality-vision-quant-q8_k_xl  window=128000  window_source=fallback  sessions=7
  metabolomics/qwen3.8-27b-text  window=128000  window_source=fallback  sessions=2
  metabolomics/qwen3.8-flash  window=262144  window_source=recorded  sessions=14
  metabolomics/qwen3.8-flash-text  window=128000  window_source=fallback  sessions=1
  metabolomics/qwen3.8-flash-vision  window=262144  window_source=recorded  sessions=4
  metabolomics/qwen3.8-flash_next  window=131072  window_source=recorded  sessions=137
  metabolomics/qwen3.8-flash_next-modality-text-quant-w3a8  window=128000  window_source=fallback  sessions=3
  metabolomics/qwen3.8-flash_next-text  window=131072  window_source=recorded  sessions=4
  metabolomics/qwen3.8-vision  window=262144  window_source=recorded  sessions=1
sessions above 100% of their window:
  OVER 100%: metabolomics/qwen3.8-27b-text used=231644 window=128000 window_source=fallback
  OVER 100%: metabolomics/qwen3.8-27b-modality-vision-quant-q8_k_xl used=154018 window=128000 window_source=fallback
  OVER 100%: metabolomics/qwen3.8-flash-text used=193802 window=128000 window_source=fallback
  OVER 100%: metabolomics/qwen3.8-27b-modality-vision-quant-q8_k_xl used=140785 window=128000 window_source=fallback

== opencode (last 30 days) ==
where a window or model limit is recorded:
  database ~/.local/share/opencode/opencode.db, message.data: none found
  database columns of session, session_context_epoch: none found
  config ~/.config/opencode/opencode.json: provider.metabolomics.models.*.limit.context [65536, 131072]
  model catalogue ~/.cache/opencode/models.json: [].models.*.limit.context 203 values, 0..99999999
models seen in recent sessions (window and window_source as the adapter resolves them):
  metabolomics/qwen3.8-27b  window=131072  window_source=recorded  sessions=1
  metabolomics/qwen3.8-27b-vision  window=65536  window_source=recorded  sessions=1
sessions above 100% of their window:
  none
```
