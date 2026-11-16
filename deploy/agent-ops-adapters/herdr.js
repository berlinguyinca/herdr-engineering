// Herdr cp-feed -> Agent Operations envelopes (pane.agent_detected / pane.agent_status_changed).
// Pure mapping; emit.js sends the result through the same ingest path as its other envelopes.
import { buildEnvelope, uint } from "./core.js";

function parse(raw) {
  try { const v = JSON.parse(raw); return v && typeof v === "object" ? v : {}; } catch { return {}; }
}

// `pane.report_metadata` tokens (display-only): "model" is a name, "ctx" a percent like "42" or "42%".
function ctxPercent(tokens) {
  const match = /^\s*(\d{1,3})\s*%?\s*$/.exec(String(tokens?.ctx ?? ""));
  return match && Number(match[1]) <= 100 ? Number(match[1]) : undefined;
}

// Herdr has no status-only Agent Operations event: working maps to tool.started and idle/done to tool.ended
// (state working/idle). blocked/unknown change nothing (no event) rather than being faked.
const STATUS_EVENT = { working: "tool.started", idle: "tool.ended", done: "tool.ended" };

export function mapHerdrAgentOps(env, { host, scope = "private" } = {}) {
  const event = env.HERDR_PLUGIN_EVENT;
  const pane = env.HERDR_PANE_ID;
  if (!pane || (event !== "pane.agent_detected" && event !== "pane.agent_status_changed")) return [];
  const ev = parse(env.HERDR_PLUGIN_EVENT_JSON);
  const server = env.HERDR_SESSION_NAME || env.HERDR_SESSION || "default";
  const sessionId = ["herdr", host, server, pane].join(":");
  const status = ev.agent_status ?? ev.status;
  const tokens = ev.tokens && typeof ev.tokens === "object" ? ev.tokens : {};
  const at = ev.occurred_at ?? ev.timestamp;
  const stamp = typeof at === "string" ? at : new Date().toISOString();
  const base = { source: "herdr-ops", scope, hostId: host, sessionId, occurredAt: stamp,
    payload: { provider: typeof (ev.agent ?? ev.process) === "string" ? (ev.agent ?? ev.process) : undefined } };
  const out = [];
  const make = (type, extra = {}) => out.push(buildEnvelope({
    ...base, ...extra, type, entityId: sessionId, key: `${sessionId}|${type}|${stamp}|${extra.status ?? ""}`,
  }));

  if (event === "pane.agent_detected") make("session.started", { status: "detected" });
  else if (STATUS_EVENT[status]) make(STATUS_EVENT[status], { status });

  const model = typeof tokens.model === "string" ? tokens.model : undefined;
  if (model) make("model.selected", { modelId: model, status: "reported" });
  const pct = ctxPercent(tokens);
  if (pct !== undefined && uint(pct) !== undefined) {
    make("context.metric", { status: "reported", payload: { context_used: pct, context_limit: 100, context_source: "herdr-ctx-pct" } });
  }
  return out;
}
