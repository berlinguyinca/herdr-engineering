// Codex -> Agent Operations envelopes from a rollout JSONL (~/.codex/sessions/**/rollout-*.jsonl) or the notify program.
// Read-only and metadata-only: only the field names below are read (session id, model, provider, tool type/name,
// call id, timestamps, numeric token counts). Messages, prompts, reasoning, tool arguments/outputs, cwd and
// instructions are never read, copied or forwarded.
import { readFileSync } from "node:fs";
import { buildEnvelope, configFromEnv, createSink, hostId, uint } from "./core.js";

const TOOL_CALLS = new Set(["function_call", "custom_tool_call", "local_shell_call"]);
const TOOL_OUTPUTS = new Set(["function_call_output", "custom_tool_output", "custom_tool_call_output", "local_shell_call_output"]);

export function parseRollout(text, { host = hostId(), scope = "private" } = {}) {
  const out = [];
  let session;
  let parent;
  let model;
  let window;
  const names = new Map();
  const base = () => ({ source: "codex-ops", scope, hostId: host, sessionId: session, entityId: session });
  const lines = text.split("\n");
  lines.forEach((line, n) => {
    let row;
    try { row = JSON.parse(line); } catch { return; }
    const p = row?.payload;
    if (!p || typeof p !== "object") return;
    const at = row.timestamp;
    const key = (k) => `${session}|${row.ordinal ?? n}|${k}`;
    if (row.type === "session_meta") {
      session = typeof p.id === "string" ? p.id : (typeof p.session_id === "string" ? p.session_id : session);
      parent = typeof p.parent_thread_id === "string" ? p.parent_thread_id : undefined;
      window = uint(p.context_window) ?? window;
      if (session) out.push(buildEnvelope({ ...base(), type: "session.started", status: "started", occurredAt: at,
        parentSessionId: parent, payload: { provider: "codex" }, key: key("start") }));
      return;
    }
    if (!session) return;
    if (row.type === "turn_context" && typeof p.model === "string" && p.model !== model) {
      out.push(buildEnvelope({ ...base(), type: model ? "model.switched" : "model.selected", modelId: p.model,
        occurredAt: at, key: key("model") }));
      model = p.model;
    } else if (row.type === "response_item" && TOOL_CALLS.has(p.type)) {
      if (typeof p.call_id === "string") names.set(p.call_id, p.name);
      out.push(buildEnvelope({ ...base(), type: "tool.started", status: "running", label: p.name, occurredAt: at,
        key: key(`tool-start|${p.call_id ?? ""}`) }));
    } else if (row.type === "response_item" && TOOL_OUTPUTS.has(p.type)) {
      out.push(buildEnvelope({ ...base(), type: "tool.ended", status: "done", label: names.get(p.call_id), occurredAt: at,
        key: key(`tool-end|${p.call_id ?? ""}`) }));
    } else if (row.type === "event_msg" && p.type === "task_complete") {
      out.push(buildEnvelope({ ...base(), type: "tool.ended", status: "turn_complete", occurredAt: at, key: key("turn") }));
    } else if (row.type === "event_msg" && p.type === "token_count" && p.info && typeof p.info === "object") {
      const used = uint(p.info.last_token_usage?.input_tokens);
      if (used !== undefined) {
        out.push(buildEnvelope({ ...base(), type: "context.metric", occurredAt: at, key: key("ctx"),
          payload: { context_used: used, context_limit: uint(p.info.model_context_window) ?? window,
            context_source: "codex-rollout" } }));
      }
    }
  });
  return out;
}

// notify program: Codex passes one JSON argument, e.g. {"type":"agent-turn-complete","thread-id":"..."}.
export function mapCodexNotify(arg, { host = hostId(), scope = "private" } = {}) {
  const note = typeof arg === "string" ? JSON.parse(arg) : arg;
  const thread = note?.["thread-id"] ?? note?.thread_id;
  if (note?.type !== "agent-turn-complete" || typeof thread !== "string") return [];
  const turn = note["turn-id"] ?? note.turn_id ?? "";
  return [buildEnvelope({ source: "codex-ops", scope, type: "tool.ended", status: "turn_complete", hostId: host,
    sessionId: thread, entityId: thread, payload: { provider: "codex" }, key: `${thread}|notify|${turn}` })];
}

export async function runCodex(argv, env = process.env, sinkOptions = {}) {
  try {
    const config = configFromEnv(env);
    if (!config) return { sent: 0 };
    const sink = createSink(config, { maxQueue: 2000, ...sinkOptions });
    const [mode, arg] = argv;
    const events = mode === "rollout" ? parseRollout(readFileSync(arg, "utf8"), { scope: config.scope })
      : mapCodexNotify(mode === "notify" ? arg : mode, { scope: config.scope });
    for (const e of events) sink.enqueue(e);
    await sink.flush();
    return sink.stats;
  } catch {
    return { sent: 0, failed: 1 };
  }
}

import { pathToFileURL } from "node:url";
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const timer = setTimeout(() => process.exit(0), 8000);
  runCodex(process.argv.slice(2)).finally(() => { clearTimeout(timer); process.exit(0); });
}
