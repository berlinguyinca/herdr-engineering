// Claude Code hooks -> Agent Operations envelopes. Reads one hook JSON object from stdin, forwards metadata only.
// model_id and context are absent (unknown): Claude hook payloads carry no context, and a model only when the
// payload itself has a `model` string. transcript_path, cwd, tool_input and tool_response are never read.
import { buildEnvelope, configFromEnv, createSink, hostId } from "./core.js";

const HOOKS = {
  SessionStart: ["session.started", "started"],
  SessionEnd: ["session.ended", "ended"],
  PostToolUse: ["tool.ended", "tool_done"],
  SubagentStart: ["session.started", "subagent_started"],
  PreCompact: ["context.compaction", "compacting"],
  Stop: ["tool.ended", "turn_complete"],        // no turn-end event type exists: returns the node to idle
};

export function mapClaudeHook(input, { host = hostId(), scope = "private" } = {}) {
  const hook = input && typeof input === "object" ? input.hook_event_name : undefined;
  const spec = HOOKS[hook];
  const session = typeof input?.session_id === "string" ? input.session_id : undefined;
  if (!spec || !session) return [];
  const [type, status] = spec;
  const sub = hook === "SubagentStart" ? (input.agent_id ?? input.tool_use_id) : undefined;
  if (hook === "SubagentStart" && typeof sub !== "string") return [];
  const sessionId = sub ? `${session}:sub:${sub}` : session;
  const stamp = typeof input.timestamp === "string" ? input.timestamp : new Date().toISOString();
  const model = typeof input.model === "string" ? input.model : (input.model?.id ?? undefined);
  return [buildEnvelope({
    source: "claude-ops", type, status, scope, hostId: host, sessionId, occurredAt: stamp,
    entityId: sessionId, parentSessionId: sub ? session : undefined,
    modelId: hook === "SessionStart" ? model : undefined,
    label: hook === "PostToolUse" ? input.tool_name : undefined,
    payload: { provider: "claude" },
    key: `${sessionId}|${hook}|${input.tool_use_id ?? ""}|${stamp}`,
  })];
}

export async function runClaudeHook(stdinText, env = process.env, sinkOptions = {}) {
  try {
    const config = configFromEnv(env);
    if (!config) return { sent: 0 };
    const sink = createSink(config, sinkOptions);
    for (const e of mapClaudeHook(JSON.parse(stdinText), { scope: config.scope })) sink.enqueue(e);
    await sink.flush();
    return sink.stats;
  } catch {
    return { sent: 0, failed: 1 };      // fail open: a hook must never break Claude
  }
}

async function main() {
  const chunks = [];
  const timer = setTimeout(() => process.exit(0), 4000);     // hard deadline
  for await (const c of process.stdin) chunks.push(c);
  await runClaudeHook(Buffer.concat(chunks).toString("utf8"));
  clearTimeout(timer);
  process.exit(0);
}

import { pathToFileURL } from "node:url";
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main().catch(() => process.exit(0));
