#!/usr/bin/env node
// HerdR control-plane feed plugin (native herdr plugin / event-hook emitter).
//
// Maps herdr lifecycle events (workspaces -> missions, panes/agents -> sessions,
// machine -> host) into the control plane's generic ingest envelope and POSTs
// them to POST /api/v1/ingest. Best-effort and non-blocking: a control-plane
// outage must never break or slow the herdr host.
//
// It is run by herdr for every subscribed event hook plus the startup hook:
//   - HERDR_PLUGIN_EVENT         event name ("startup", "workspace.created", ...)
//   - HERDR_PLUGIN_EVENT_JSON    event payload (JSON string) for event hooks
//   - HERDR_WORKSPACE_ID / HERDR_TAB_ID / HERDR_PANE_ID
//   - HERDR_PLUGIN_CONFIG_DIR    plugin config dir (user-editable config lives here)
//   - HOSTNAME                   host identity
//
// Control-plane URL resolution order:
//   1. file <HERDR_PLUGIN_CONFIG_DIR>/cp-url
//   2. env HERDR_CP_URL
//   3. default http://dev.lan
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

export const CONTROL_PLANE_DEFAULT = "http://dev.lan";

const EVENT_JSON = "HERDR_PLUGIN_EVENT_JSON";

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

function hostEnvelope(host, eventType, extra = {}) {
  return {
    entity_type: "host",
    entity_id: host,
    event_type: eventType,
    payload: { host_name: host, ...extra },
  };
}

function missionEnvelope(ws, eventType, { title, stage, purpose } = {}) {
  return {
    entity_type: "mission",
    entity_id: ws,
    event_type: eventType,
    payload: {
      title: title ?? ws,
      purpose: purpose ?? "",
      stage: stage ?? "active",
    },
  };
}

function sessionEnvelope(pane, eventType, { host, ws, role, status } = {}) {
  return {
    entity_type: "session",
    entity_id: pane,
    event_type: eventType,
    payload: {
      host_id: host,
      mission_id: ws,
      agent_role: role ?? "",
      status: status ?? "active",
    },
  };
}

// Pure mapping from herdr runtime env -> list of control-plane ingest envelopes.
export function mapEvent(env) {
  const event = env.HERDR_PLUGIN_EVENT ?? "";
  const host = env.HOSTNAME ?? "unknown-host";
  const ws = env.HERDR_WORKSPACE_ID;
  const pane = env.HERDR_PANE_ID;
  const ev = eventJson(env);
  const out = [];

  if (event === "startup") {
    out.push(hostEnvelope(host, "host.registered"));
    return out;
  }

  // host heartbeat: any focused/created event proves the host is alive.
  if (event === "workspace.focused" || event === "workspace.created") {
    out.push(hostEnvelope(host, "host.heartbeat"));
  }

  // mission events (a herdr workspace is a mission).
  if (ws) {
    if (event === "workspace.created") {
      out.push(missionEnvelope(ws, "mission.started", { title: ev.name ?? ws }));
    } else if (event === "workspace.renamed") {
      out.push(
        missionEnvelope(ws, "mission.updated", { title: ev.name ?? ws }),
      );
    } else if (event === "workspace.focused") {
      out.push(missionEnvelope(ws, "mission.updated", { stage: "active" }));
    } else if (event === "workspace.closed") {
      out.push(missionEnvelope(ws, "mission.ended", { stage: "complete" }));
    }
  }

  // session events (a pane is an agent session).
  if (pane) {
    if (event === "pane.created" || event === "pane.agent_detected") {
      out.push(
        sessionEnvelope(pane, "session.started", {
          host,
          ws,
          role: ev.agent_kind ?? ev.agent_role ?? "",
        }),
      );
    } else if (event === "pane.agent_status_changed") {
      out.push(
        sessionEnvelope(pane, "session.updated", {
          host,
          ws,
          role: ev.agent_kind ?? ev.agent_role ?? "",
          status: ev.status ?? "active",
        }),
      );
    } else if (event === "pane.focused") {
      out.push(sessionEnvelope(pane, "session.updated", { host, ws }));
    } else if (event === "pane.exited" || event === "pane.closed") {
      out.push(
        sessionEnvelope(pane, "session.terminated", {
          host,
          ws,
          role: ev.agent_kind ?? ev.agent_role ?? "",
          status: "stopped",
        }),
      );
    }
  }

  return out;
}

// Control-plane base URL. Config file beats env beats default.
export function resolveUrl(env) {
  const dir = env.HERDR_PLUGIN_CONFIG_DIR;
  if (dir) {
    try {
      const fromFile = readFileSync(join(dir, "cp-url"), "utf8").trim();
      if (fromFile) return fromFile;
    } catch {
      // fall through
    }
  }
  return env.HERDR_CP_URL || CONTROL_PLANE_DEFAULT;
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

async function main() {
  const env = process.env;
  const envelopes = mapEvent(env);
  if (envelopes.length === 0) return;
  const url = resolveUrl(env);
  // Fire-and-forget each envelope in parallel; never throw to herdr.
  await Promise.allSettled(envelopes.map((e) => send(url, e)));
}

// Only run when executed directly (not when imported by tests).
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    process.stderr.write(`[herdr-cp-feed] ${err}\n`);
  });
}

export { main };
