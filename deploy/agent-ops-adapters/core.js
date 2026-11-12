// Shared core of the Agent Operations provider adapters: sanitize step, envelope builder, fail-open sink.
// Metadata only. Nothing here reads or forwards prompts, messages, file contents, paths, args or secrets.
import { createHash } from "node:crypto";
import os from "node:os";

export const EVENT_TYPES = new Set([
  "session.started", "session.ended", "model.selected", "model.switched", "tool.started", "tool.ended",
  "tool.failed", "context.metric", "context.compaction", "agent.disconnected", "agent.reconnected",
]);
const SCOPES = new Set(["private", "mission", "room", "org"]);
const LABEL_MAX = 80;
// Same semantics as the backend's envelope.sanitize_label (the server re-applies it).
const REDACTIONS = [
  [/\b(?:authorization:\s*)?bearer\s+\S+/gi, "[redacted]"],
  [/\b[\w-]*(?:password|passwd|secret|token|api[_-]?key)[\w-]*\s*[=:]\s*\S+/gi, "[redacted]"],
  [/\b(?:sk|pk|ghp|gho|ghs|xox[abp])[-_][A-Za-z0-9_-]{8,}/g, "[redacted]"],
  [/\bAKIA[0-9A-Z]{12,}\b/g, "[redacted]"],
  [/\b[A-Za-z0-9+/_-]{32,}={0,2}/g, "[redacted]"],
  [/[A-Za-z]:\\\S+/g, "[path]"],
  [/(?<![\w:])(?:~|\.{1,2})?(?:\/[^\s/]+)+\/?/g, "[path]"],
];

export function sanitizeLabel(value) {
  if (typeof value !== "string" || !value) return undefined;
  let out = value;
  for (const [re, rep] of REDACTIONS) out = out.replace(re, rep);
  out = out.split(/\s+/).filter(Boolean).join(" ");
  if (!out) return undefined;
  return out.length <= LABEL_MAX ? out : out.slice(0, LABEL_MAX - 1) + "…";
}

// Ids (session, model, host): short tokens only; anything path- or secret-shaped is dropped (stays unknown).
export function sanitizeId(value, max = 160) {
  if (typeof value !== "string" || !value || value.length > max) return undefined;
  if (!/^[A-Za-z0-9][A-Za-z0-9._:@+/-]*$/.test(value)) return undefined;
  if ((value.match(/\//g) || []).length > 1 || /\.\.|\/\/|^[A-Za-z0-9]{32,}$/.test(value)) return undefined;
  return value;
}

export function sanitizeStatus(value) {
  return typeof value === "string" && /^[a-z][a-z0-9_.-]{0,63}$/.test(value) ? value : undefined;
}

export function uint(value) {
  return Number.isInteger(value) && value >= 0 ? value : undefined;
}

export function hostId(env = process.env) {
  const override = env.HOSTNAME;
  if (override && override !== "unknown-host") return sanitizeId(override);
  try { return sanitizeId(os.hostname()); } catch { return undefined; }
}

// Disabled unless explicitly configured: HERDR_AGENT_OPS=1 and a control-plane URL.
export function configFromEnv(env = process.env) {
  const url = env.HERDR_AGENT_OPS_URL || "";
  if (env.HERDR_AGENT_OPS !== "1" || !/^https?:\/\//.test(url)) return null;
  const scope = SCOPES.has(env.HERDR_AGENT_OPS_SCOPE) ? env.HERDR_AGENT_OPS_SCOPE : "private";
  return {
    url: url.replace(/\/+$/, ""), apiKey: env.HERDR_AGENT_OPS_API_KEY || null, scope,
    timeoutMs: Number(env.HERDR_AGENT_OPS_TIMEOUT_MS) || 1500,
  };
}

let lastSequence = 0;   // per process: source_sequence strictly increases in build (= emit) order, even within one ms

// Build one envelope. Absent/invalid optional fields are omitted (unknown), never guessed.
// `key` makes event_id deterministic so a redelivery of the same observation dedupes.
export function buildEnvelope({ source, type, key, entityId, occurredAt, scope = "private", provenance = "observed",
  hostId: host, sessionId, agentId, parentSessionId, modelId, status, label, payload }) {
  if (!EVENT_TYPES.has(type)) throw new Error(`unsupported event type ${type}`);
  const digest = createHash("sha256").update(`${source}\n${type}\n${key}`).digest("hex");
  const when = typeof occurredAt === "string" && !Number.isNaN(Date.parse(occurredAt))
    ? new Date(occurredAt).toISOString() : new Date().toISOString();
  const env = {
    schema_version: 2, event_id: `ao-${digest.slice(0, 32)}`, source,
    // ms timestamp plus a digest-derived slot (keeps concurrent hook processes apart), forced above the previous one
    // from this process so events built in the same ms keep their emit order (tool.started before tool.ended).
    source_sequence: (lastSequence = Math.max(lastSequence + 1,
      Date.parse(when) * 1000 + (parseInt(digest.slice(0, 4), 16) % 1000))),
    entity_type: "lifecycle", entity_id: sanitizeId(entityId) || sanitizeId(sessionId) || "unknown",
    event_type: type, occurred_at: when, visibility_scope: scope, provenance,
  };
  const optional = {
    host_id: sanitizeId(host, 512), session_id: sanitizeId(sessionId, 512), agent_id: sanitizeId(agentId, 512),
    parent_session_id: sanitizeId(parentSessionId, 512), model_id: sanitizeId(modelId, 128),
    status: sanitizeStatus(status), sanitized_label: sanitizeLabel(label),
  };
  for (const [k, v] of Object.entries(optional)) if (v !== undefined) env[k] = v;
  if (payload) {
    const clean = {};
    for (const k of ["context_used", "context_limit"]) if (uint(payload[k]) !== undefined) clean[k] = payload[k];
    for (const k of ["context_source", "provider"]) {
      const v = sanitizeId(payload[k], 64);
      if (v !== undefined) clean[k] = v;
    }
    if (Object.keys(clean).length) env.payload = clean;
  }
  return env;
}

// Fail-open sink: bounded queue (oldest dropped), per-request timeout, never throws, never blocks the caller
// unless `flush()` is awaited (hook CLIs do, with a hard deadline; long-lived hosts never do).
export function createSink(config, { maxQueue = 64, fetchImpl = globalThis.fetch } = {}) {
  const queue = [];
  const stats = { sent: 0, failed: 0, dropped: 0 };
  let flushing = null;

  async function post(envelope) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), config.timeoutMs);
    try {
      const headers = { "content-type": "application/json" };
      if (config.apiKey) headers["X-API-Key"] = config.apiKey;
      const res = await fetchImpl(`${config.url}/api/v1/ingest`, {
        method: "POST", headers, body: JSON.stringify(envelope), signal: controller.signal,
      });
      res.ok ? stats.sent++ : stats.failed++;
    } catch {
      stats.failed++;
    } finally {
      clearTimeout(timer);
    }
  }

  async function drain() {
    while (queue.length) await post(queue.shift());
  }

  return {
    stats,
    get size() { return queue.length; },
    enqueue(envelope) {
      try {
        if (queue.length >= maxQueue) { queue.shift(); stats.dropped++; }
        queue.push(envelope);
      } catch { stats.dropped++; }
    },
    flush() {
      flushing = (flushing || Promise.resolve()).then(drain).catch(() => {});
      return flushing;
    },
  };
}
