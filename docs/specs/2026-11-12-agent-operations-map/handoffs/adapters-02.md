# Adapters handoff: slice 02

Branch `feat/agent-ops-adapters`. Adapters live in `deploy/agent-ops-adapters/` (ESM, no dependencies) and post to the
existing `POST /api/v1/ingest` (envelope v2, `entity_type: lifecycle`); no new bus, no backend change.

## Enabling (all adapters are inert by default)
`HERDR_AGENT_OPS=1` and `HERDR_AGENT_OPS_URL=<control plane>` are both required. Optional: `HERDR_AGENT_OPS_API_KEY`
(an `ingest` role key), `HERDR_AGENT_OPS_SCOPE=private|org|room|mission` (default `private`, fail closed: only the
operator sees it; set `org` for the shared map), `HERDR_AGENT_OPS_TIMEOUT_MS` (default 1500).
Nothing is installed into Herdr, Claude, Codex or Pi by this slice.

| Provider | Entry point | Wire it with |
|---|---|---|
| Herdr | `deploy/herdr-cp-feed-plugin/emit.js` (dynamic import of `herdr.js`, only when `HERDR_AGENT_OPS=1`; URL and key come from the existing host.json/fleet config) | already hooked on `pane.agent_detected` / `pane.agent_status_changed` |
| Pi | `deploy/agent-ops-adapters/pi.js` (default export = Pi extension) | `pi install` the file, set env |
| Claude | `node deploy/agent-ops-adapters/claude.js` reads one hook JSON on stdin | `SessionStart, PostToolUse, SubagentStart, PreCompact, Stop` (and optional `SessionEnd`) hook commands |
| Codex | `node codex.js rollout <rollout.jsonl>` or `node codex.js notify '<json>'` | `notify = [...]` in `~/.codex/config.toml`, or run `rollout` on a finished/tailed file |

## What each provider can and cannot report
| Field | Herdr | Pi | Claude | Codex |
|---|---|---|---|---|
| session identity | `herdr:<host>:<server>:<pane>` | Pi session id (else a pid/boot hash) | hook `session_id` | rollout `session_meta.id` / notify `thread-id` |
| host_id | yes | yes | yes | yes |
| provenance | observed | observed | observed | observed |
| state | working, idle/done only | tool start/end, error -> attention | tool end, Stop -> idle | tool start/end, task_complete -> idle |
| blocked/waiting | no (no event type; skipped, not faked) | no | no | no |
| model_id | only when `tokens.model` is in the event | `model_select` (switch tracked) | only if the hook payload has `model` (SessionStart); otherwise null | `turn_context.model` (switch tracked) |
| context | only `tokens.ctx` percent (used=pct, limit=100, source `herdr-ctx-pct`) | `getContextUsage()` only when tokens non-null; null after a prior metric -> compaction, never a fake metric | never (null) | `token_count.last_token_usage.input_tokens` / `model_context_window` when present, else null |
| compaction | no | `session_compact`, or null usage after a metric | `PreCompact` | no |
| subagents | no | no | `SubagentStart` -> child session with `parent_session_id` | `parent_thread_id` -> `parent_session_id` |
| tool identity | no | tool name as label | tool name as label | tool/function name as label |
| never read | | | `transcript_path`, `cwd`, `tool_input`, `tool_response` | messages, reasoning, arguments, outputs, cwd, instructions |

Shared sanitize step (`core.js`): ids must be short tokens (paths/secret-shaped values are dropped so the field stays
absent), labels use the backend's `sanitize_label` redactions, payload is limited to the whitelisted numeric/context keys,
the server re-sanitizes. Event ids are a digest of (source, type, observation key) so redelivery dedupes server side;
`source_sequence` is `epoch_ms*1000 + digest slot`.

Fail-open: every adapter swallows all errors; the sink has a bounded queue (64, 2000 for rollout parsing; oldest
dropped), a per-request timeout, and hook CLIs have a hard 4s/8s exit deadline and always exit 0. Pi handlers are
try/catch-wrapped and delivery is never awaited by a hook.

## Commits
See `git log feat/agent-ops-adapters` (prefix `agent-ops-adapters:`).

## Tests and results
`/private/tmp/claude-501/aoenv/bin/python -m pytest tests/control_plane/test_agent_ops_adapters.py -q -p no:cacheprovider`
-> 16 passed. Also `tests/control_plane/test_agent_ops.py` (22 passed) and `node --test emit.test.js emit-send.test.js`
in `deploy/herdr-cp-feed-plugin` (34 passed). Real in-process control plane server, real `node` subprocesses (Claude hook
CLI, Codex CLI, `emit.js`); Pi is exercised by calling the registered handlers with real `getContextUsage` functions
(no Pi runtime is available here). Covered: snapshot shows each agent with provenance observed and unknown fields null,
duplicate delivery dedupes, private scope hidden from viewers, disabled-by-default, dead control plane / bad input / throwing
host callbacks never raise, bounded queue, no content/path leakage, rollout file unmodified.

## Limitations
- Herdr has no status-only event type: working -> `tool.started`, idle/done -> `tool.ended`; blocked is dropped. A
  `session.status` event type in `agent_ops.py` would fix this (owned elsewhere).
- Claude `Stop` maps to `tool.ended` (status `turn_complete`) to return the node to idle, for the same reason.
- Herdr `ctx` token is a display percentage, not a token count; it is stored as used=pct/limit=100.
- Codex `context_used` is the last request's `input_tokens`, an approximation of the live window.
- Pi hook payload shapes (`model_select`, `getContextUsage()` fields `tokens`/`contextWindow`) follow DISCOVERY-B and
  are read defensively; not run against a live Pi.
- Claude hook payload `model` is read if present; current Claude Code versions may or may not send it.
- `emit.js` imports `../agent-ops-adapters/herdr.js`; a plugin copy without that directory silently emits no Agent
  Operations events (existing events unaffected).
- Codex rollout is parsed whole per invocation (no tailing offset); re-runs dedupe by event id.
