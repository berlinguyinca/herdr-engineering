#!/usr/bin/env node
// HerdR control-plane feed plugin (native herdr plugin / event-hook emitter).
//
// Maps herdr lifecycle events into the control plane's generic ingest envelope
// and POSTs them to POST /api/v1/ingest. Best-effort and non-blocking: a
// control-plane outage must never break or slow the herdr host.
//
// Entity mapping:
//   machine (os.hostname())   -> host  (host.registered / host.heartbeat)
//   workspace                 -> mission (started/updated/ended)
//   pane running an agent     -> session (started/updated/terminated)
//
// The mission title is resolved to the workspace's human label (custom_name in
// session.json, e.g. "herdr frontend work" for workspace "wK") so the UI shows
// real scope instead of a cryptic id. Only panes where herdr detects an agent
// become sessions (pane.agent_detected), so the dashboard shows actual agents,
// not every terminal pane.
//
// Env provided by herdr:
//   HERDR_PLUGIN_EVENT       event name ("startup", "workspace.created", ...)
//   HERDR_PLUGIN_EVENT_JSON  event payload (JSON string) for event hooks
//   HERDR_WORKSPACE_ID / HERDR_TAB_ID / HERDR_PANE_ID
//   HERDR_PLUGIN_CONFIG_DIR  plugin config dir
//   HERDR_PLUGIN_STATE_DIR   plugin state dir
//
// Control-plane URL: control_plane_url from ~/.config/herdr-engineering/host.json
// (written by `herdr-eng fleet join`). No env var or built-in default; a host
// that has not joined emits nothing.
import os from "node:os";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { readHostJson } from "./host-config.js";
import { pathToFileURL } from "node:url";
import { enrichSession, resolveSessionEnv } from "./enrich.js";
import { recordSessionBinding } from "./session-helpers.js";

const EVENT_JSON = "HERDR_PLUGIN_EVENT_JSON";

// Host identity: os.hostname() is authoritative; HOSTNAME env (which herdr does
// not set) is only a fallback override for testing.
export function hostIdentity(env) {
  const override = env.HOSTNAME;
  if (override && override !== "unknown-host") return override;
  try {
    return os.hostname() || "unknown-host";
  } catch {
    return "unknown-host";
  }
}

// Locate herdr's session.json (workspace metadata). Tried in order.
export function sessionJsonPath(env) {
  const candidates = [];
  if (env.HERDR_SESSION_JSON) candidates.push(env.HERDR_SESSION_JSON);
  if (env.HERDR_PLUGIN_CONFIG_DIR) {
    // <herdr-config>/plugins/config/<id> -> up 3 = <herdr-config>
    candidates.push(join(env.HERDR_PLUGIN_CONFIG_DIR, "../../../session.json"));
  }
  candidates.push(join(os.homedir(), ".config", "herdr", "session.json"));
  return candidates.find((p) => existsSync(p)) || null;
}

// Resolve a workspace's human label + project dir. Sources, in order:
//   1. session.json (persistent id -> custom_name / identity_cwd)
//   2. the invocation context (workspace_label / workspace_cwd for the active
//      workspace, injected by herdr into HERDR_PLUGIN_CONTEXT_JSON)
//   3. fall back to the id / undefined
// `ctx` is the parsed HERDR_PLUGIN_CONTEXT_JSON (see contextJson).
export function workspaceInfo(env, wsId, ctx = {}) {
  const path = sessionJsonPath(env);
  if (path) {
    try {
      const d = JSON.parse(readFileSync(path, "utf8"));
      const list = Array.isArray(d?.workspaces) ? d.workspaces : [];
      const w = list.find((x) => x.id === wsId || x.workspace_id === wsId);
      if (w) {
        return {
          title: w.custom_name || w.label || w.name || wsId,
          cwd: w.identity_cwd || undefined,
        };
      }
    } catch {
      // fall through
    }
  }
  if (ctx.workspace_label) return { title: ctx.workspace_label, cwd: ctx.workspace_cwd || undefined };
  return { title: wsId, cwd: undefined };
}

function eventJson(env) {
  const raw = env[EVENT_JSON];
  if (!raw) return {};
  try {
    const v = JSON.parse(raw);
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
}

// The plugin invocation context injected by herdr: includes the active
// workspace's label/cwd and the focused pane's agent (when available).
export function contextJson(env) {
  const raw = env.HERDR_PLUGIN_CONTEXT_JSON;
  if (!raw) return {};
  try {
    const v = JSON.parse(raw);
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
}

// Best-effort extraction of an agent's role/kind. Herdr keys agent detection by
// the pane's process name (see herdr-server.log `process=pi`), so the event JSON
// usually carries `process` rather than `agent`. Also check the event's `agent`
// field and the invocation context's focused-pane agent as fallbacks.
function agentRole(ev, ctx = {}) {
  return ev.agent_kind ?? ev.agent_name ?? ev.agent ?? ev.process
    ?? ev.kind ?? ev.role ?? ev.agent_role
    ?? ctx.focused_pane_agent
    ?? (ctx.agent && ctx.agent.name) ?? "";
}

function hostEnvelope(host, eventType, extra = {}) {
  return {
    entity_type: "host",
    entity_id: host,
    event_type: eventType,
    payload: { host_name: host, ...extra },
  };
}

function missionEnvelope(ws, eventType, info, { stage, purpose } = {}) {
  return {
    entity_type: "mission",
    entity_id: ws,
    event_type: eventType,
    payload: {
      title: info.title ?? ws,
      purpose: purpose ?? info.purpose ?? "",
      cwd: info.cwd,
      stage: stage ?? "active",
    },
  };
}

function sessionEnvelope(pane, eventType, { host, ws, role, status, server, serverKey } = {}) {
  return {
    entity_type: "session",
    entity_id: ["herdr", host, serverKey, pane].map(encodeURIComponent).join(":"),
    event_type: eventType,
    payload: {
      host_id: host,
      pane_id: pane,
      herdr_session: server,
      mission_id: ws,
      agent_role: role ?? "",
      status: status ?? "active",
    },
  };
}

// Pure mapping from herdr runtime env -> list of control-plane ingest envelopes.
// `ctx` lets callers inject the resolved host + workspace metadata so this stays
// free of I/O and deterministic for tests:
//   ctx.host  - resolved host identity (default: hostIdentity(env))
//   ctx.info  - { [workspaceId]: { title, cwd } } (default: resolved from env)
export function mapEvent(env, ctx = {}) {
  const event = env.HERDR_PLUGIN_EVENT ?? "";
  const host = ctx.host || hostIdentity(env);
  const ws = env.HERDR_WORKSPACE_ID;
  const pane = env.HERDR_PANE_ID;
  const ev = eventJson(env);
  const envCtx = contextJson(env);
  const server = env.HERDR_SESSION_NAME || env.HERDR_SESSION || envCtx.session_name || null;
  // An unknown server remains unusable for commands. Its config directory
  // still separates telemetry from distinct named servers on the same host.
  const serverKey = server || env.HERDR_PLUGIN_CONFIG_DIR || "unknown";
  const info = (id) => (ctx.info && ctx.info[id])
    || workspaceInfo(env, id, envCtx);
  const role = () => agentRole(ev, envCtx);
  const out = [];

  if (event === "startup") {
    out.push(hostEnvelope(host, "host.registered"));
    return out;
  }

  // host heartbeat: a workspace focused/created proves the host is alive.
  if (event === "workspace.focused" || event === "workspace.created") {
    out.push(hostEnvelope(host, "host.heartbeat"));
  }

  // mission events (a herdr workspace is a mission).
  if (ws) {
    if (event === "workspace.created") {
      out.push(
        missionEnvelope(ws, "mission.started", info(ws),
          { title: ev.name ?? ev.label ?? info(ws).title,
            purpose: ev.purpose ?? "" }),
      );
    } else if (event === "workspace.renamed") {
      out.push(
        missionEnvelope(ws, "mission.updated", info(ws),
          { title: ev.name ?? ev.label ?? info(ws).title }),
      );
    } else if (event === "workspace.focused") {
      out.push(missionEnvelope(ws, "mission.updated", info(ws)));
    } else if (event === "workspace.closed") {
      out.push(missionEnvelope(ws, "mission.ended", info(ws), {
        stage: "complete",
      }));
    }
  }

  // session events: only panes running an agent become sessions.
  if (pane) {
    if (event === "pane.agent_detected") {
      out.push(
        sessionEnvelope(pane, "session.started", {
          host,
          server,
          serverKey,
          ws,
          role: role(),
        }),
      );
    } else if (event === "pane.agent_status_changed") {
      out.push(
        sessionEnvelope(pane, "session.updated", {
          host,
          server,
          serverKey,
          ws,
          role: role(),
          status: ev.agent_status ?? ev.status ?? "active",
        }),
      );
    } else if (event === "pane.exited" || event === "pane.closed") {
      out.push(
        sessionEnvelope(pane, "session.terminated", {
          host,
          server,
          serverKey,
          ws,
          role: role(),
          status: "stopped",
        }),
      );
    }
  }

  return out;
}

// Control-plane base URL from host.json, or null when this host has not joined.
export function resolveUrl(env = process.env) {
  try { return readHostJson(env.HOME || os.homedir()).url; }
  catch { return null; }
}

async function send(url, envelope) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 2000);
  try {
    const res = await fetch(`${url}/api/v1/ingest`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(envelope),
      signal: controller.signal,
    });
    if (!res.ok) {
      process.stderr.write(
        `[herdr-cp-feed] ingest ${res.status} for ${envelope.entity_type}:${envelope.entity_id}\n`,
      );
    }
  } catch (err) {
    process.stderr.write(
      `[herdr-cp-feed] ingest failed for ${envelope.entity_type}:${envelope.entity_id}: ${err}\n`,
    );
  } finally {
    clearTimeout(timer);
  }
}

// Resolve workspace metadata for every workspace id referenced by an event.
function resolveContext(env, envelopes) {
  const envCtx = contextJson(env);
  const ids = [...new Set(
    envelopes.filter((e) => e.entity_type === "mission").map((e) => e.entity_id))];
  const info = {};
  for (const id of ids) info[id] = workspaceInfo(env, id, envCtx);
  return info;
}

async function main() {
  const env = await resolveSessionEnv(process.env).catch(() => process.env);
  const host = hostIdentity(env);
  const base = mapEvent(env, { host });
  const info = resolveContext(env, base);
  const envelopes = info && Object.keys(info).length
    ? mapEvent(env, { host, info })
    : base;
  if (envelopes.length === 0) return;
  // Enrich session envelopes with live detail (cwd/branch/pid/summary/attach)
  // via the herdr CLI + git, then send everything. Enrichment is best-effort.
  const enriched = await Promise.all(
    envelopes.map((e) => enrichSession(env, e).catch(() => e)));
  // Retain exact identities locally before any network request. The snapshot
  // companion can drain a short session even after its pane has disappeared.
  for (const envelope of enriched) recordSessionBinding(envelope);
  const url = resolveUrl(env);
  if (!url) {
    process.stderr.write("[herdr-cp-feed] host config unavailable: no host.json; run `herdr-eng fleet join`\n");
    return;
  }
  await Promise.allSettled(enriched.map((e) => send(url, e)));
}

// Only run when executed directly (not when imported by tests).
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    process.stderr.write(`[herdr-cp-feed] ${err}\n`);
  });
}

export { main };
