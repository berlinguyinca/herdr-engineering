// Pi extension -> Agent Operations envelopes. Install like any Pi extension (`pi install`); inert unless
// HERDR_AGENT_OPS=1 and HERDR_AGENT_OPS_URL are set. Every handler is wrapped: nothing here can throw into Pi,
// and delivery is queued and never awaited by a hook.
import { createHash } from "node:crypto";
import { buildEnvelope, configFromEnv, createSink, hostId, uint } from "./core.js";

function modelOf(event, ctx) {
  const m = event?.model ?? ctx?.model;
  const id = typeof m === "string" ? m : (m?.id ?? event?.modelId);
  const provider = typeof m === "object" ? m?.provider : undefined;
  return typeof id === "string" ? (provider && !id.includes("/") ? `${provider}/${id}` : id) : undefined;
}

export function createPiAdapter(pi, { env = process.env, sinkOptions = {}, sessionIdOf } = {}) {
  const config = configFromEnv(env);
  if (!config) return null;
  const sink = createSink(config, sinkOptions);
  const host = hostId(env);
  const boot = new Date().toISOString();
  let seq = 0;
  let lastModel;
  let hadMetric = false;

  const safe = (fn) => (event, ctx) => {
    try { fn(event ?? {}, ctx ?? {}); sink.flush(); } catch { /* fail open */ }
  };
  const sessionId = (ctx) => {
    const real = sessionIdOf?.(ctx) ?? ctx?.sessionManager?.getSessionId?.() ?? ctx?.sessionId;
    return typeof real === "string" && real
      ? real : `pi-${createHash("sha256").update(`${host}|${process.pid}|${boot}`).digest("hex").slice(0, 16)}`;
  };
  const emit = (type, ctx, extra = {}) => {
    const sid = sessionId(ctx);
    sink.enqueue(buildEnvelope({
      source: "pi-ops", scope: config.scope, type, hostId: host, sessionId: sid, entityId: sid,
      payload: { provider: "pi", ...extra.payload }, ...extra, key: `${sid}|${type}|${++seq}|${boot}`,
    }));
  };
  const metric = (ctx) => {
    const usage = ctx?.getContextUsage?.();
    const used = uint(usage?.tokens);
    if (used !== undefined) {
      hadMetric = true;
      const limit = uint(usage.contextWindow);
      emit("context.metric", ctx, { payload: { context_used: used, context_limit: limit || undefined, context_source: "pi" } });
    } else if (usage == null && hadMetric) {
      hadMetric = false;
      emit("context.compaction", ctx);     // null after compaction: no fake metric
    }
  };

  pi.on("session_start", safe((e, ctx) => emit("session.started", ctx, { status: "started" })));
  pi.on("session_shutdown", safe((e, ctx) => emit("session.ended", ctx, { status: "ended" })));
  pi.on("model_select", safe((e, ctx) => {
    const model = modelOf(e, ctx);
    if (!model) return;
    emit(lastModel && lastModel !== model ? "model.switched" : "model.selected", ctx, { modelId: model });
    lastModel = model;
  }));
  pi.on("tool_execution_start", safe((e, ctx) => emit("tool.started", ctx, { status: "running", label: e.toolName })));
  pi.on("tool_execution_end", safe((e, ctx) =>
    emit(e.isError ? "tool.failed" : "tool.ended", ctx, { status: e.isError ? "error" : "ok", label: e.toolName })));
  pi.on("message_end", safe((e, ctx) => metric(ctx)));
  pi.on("session_compact", safe((e, ctx) => { hadMetric = false; emit("context.compaction", ctx); }));
  return { sink };
}

export default function (pi) {
  try { createPiAdapter(pi); } catch { /* fail open */ }
}
