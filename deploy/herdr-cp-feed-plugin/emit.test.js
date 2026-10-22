// Unit tests for the HerdR control-plane feed plugin's event→envelope mapping.
//
// Run with:  node --test emit.test.js   (Node 18+)
import { test } from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  mapEvent,
  resolveUrl,
  hostIdentity,
  sessionJsonPath,
  workspaceInfo,
} from "./emit.js";

const HOST = os.hostname();

function env(overrides = {}) {
  return { ...overrides };
}

const INFO = {
  wK: { title: "herdr frontend work", cwd: "/home/wohlgemuth/IdeaProjects" },
  w1H: { title: "wcmc - release", cwd: "/home/wohlgemuth/IdeaProjects/wcmc3" },
};

// ---------------------------------------------------------------------------
// host
// ---------------------------------------------------------------------------
test("host identity falls back to os.hostname (herdr does not set HOSTNAME)", () => {
  assert.equal(hostIdentity({}), os.hostname());
  assert.notEqual(hostIdentity({}), "unknown-host");
  // explicit override wins, except the bogus "unknown-host" literal
  assert.equal(hostIdentity({ HOSTNAME: "beast" }), "beast");
  assert.equal(hostIdentity({ HOSTNAME: "unknown-host" }), os.hostname());
});

test("startup emits a single host.registered envelope", () => {
  const out = mapEvent(env({ HERDR_PLUGIN_EVENT: "startup" }));
  assert.equal(out.length, 1);
  assert.deepEqual(out[0], {
    entity_type: "host",
    entity_id: HOST,
    event_type: "host.registered",
    payload: { host_name: HOST },
  });
});

// ---------------------------------------------------------------------------
// mission (workspace)
// ---------------------------------------------------------------------------
test("workspace.focused emits host.heartbeat + mission.updated", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "workspace.focused", HERDR_WORKSPACE_ID: "wK" }),
    { host: HOST, info: INFO },
  );
  const types = out.map((e) => e.event_type);
  assert.ok(types.includes("host.heartbeat"));
  assert.ok(types.includes("mission.updated"));
});

test("workspace.created emits mission.started with the resolved human label", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "workspace.created", HERDR_WORKSPACE_ID: "wK" }),
    { host: HOST, info: INFO },
  );
  const m = out.find((e) => e.entity_type === "mission");
  assert.ok(m);
  assert.equal(m.entity_id, "wK");
  assert.equal(m.event_type, "mission.started");
  assert.equal(m.payload.title, "herdr frontend work"); // NOT "wK"
  assert.equal(m.payload.cwd, "/home/wohlgemuth/IdeaProjects");
  assert.equal(m.payload.stage, "active");
});

test("workspace label falls back to the id when session.json lacks it", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "workspace.created", HERDR_WORKSPACE_ID: "wX" }),
    { host: HOST, info: {} },
  );
  const m = out.find((e) => e.entity_type === "mission");
  assert.equal(m.payload.title, "wX");
});

test("workspace.closed emits mission.ended with stage complete", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "workspace.closed", HERDR_WORKSPACE_ID: "wK" }),
    { host: HOST, info: INFO },
  );
  const m = out.find((e) => e.entity_type === "mission");
  assert.equal(m.event_type, "mission.ended");
  assert.equal(m.payload.stage, "complete");
});

// ---------------------------------------------------------------------------
// session (agent pane)
// ---------------------------------------------------------------------------
test("pane.agent_detected emits session.started with the agent role", () => {
  const out = mapEvent(
    env({
      HERDR_PLUGIN_EVENT: "pane.agent_detected",
      HERDR_WORKSPACE_ID: "wK",
      HERDR_PANE_ID: "wK:p1",
      HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ agent_kind: "pi" }),
    }),
    { host: HOST, info: INFO },
  );
  const s = out.find((e) => e.entity_type === "session");
  assert.ok(s);
  assert.equal(s.entity_id, `herdr:${HOST}:unknown:wK%3Ap1`);
  assert.equal(s.event_type, "session.started");
  assert.equal(s.payload.host_id, HOST);
  assert.equal(s.payload.mission_id, "wK");
  assert.equal(s.payload.agent_role, "pi");
  assert.equal(s.payload.status, "active");
});

test("pane.agent_status_changed emits session.updated with status", () => {
  const out = mapEvent(
    env({
      HERDR_PLUGIN_EVENT: "pane.agent_status_changed",
      HERDR_PANE_ID: "wK:p1",
      HERDR_WORKSPACE_ID: "wK",
      HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ status: "blocked" }),
    }),
    { host: HOST, info: INFO },
  );
  const s = out.find((e) => e.entity_type === "session");
  assert.equal(s.event_type, "session.updated");
  assert.equal(s.payload.status, "blocked");
});

test("pane.agent_detected reads the agent process name when no agent field", () => {
  const out = mapEvent(
    env({
      HERDR_PLUGIN_EVENT: "pane.agent_detected",
      HERDR_PANE_ID: "w1H:p5",
      HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ process: "pi", agent_status: "working" }),
    }),
    { host: HOST, info: INFO },
  );
  const s = out.find((e) => e.entity_type === "session");
  assert.equal(s.payload.agent_role, "pi");
});

test("pane.agent_detected reads the agent name from the event JSON", () => {
  const out = mapEvent(
    env({
      HERDR_PLUGIN_EVENT: "pane.agent_detected",
      HERDR_WORKSPACE_ID: "w1H",
      HERDR_PANE_ID: "w1H:p5",
      HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ agent: "Pi", agent_status: "working" }),
    }),
    { host: HOST, info: INFO },
  );
  const s = out.find((e) => e.entity_type === "session");
  assert.equal(s.payload.agent_role, "Pi");
});

// The invocation context carries focused_pane_agent / workspace_label when the
// event JSON itself is sparse (e.g. no agent field on agent_detected).
test("agent role falls back to focused_pane_agent from the context JSON", () => {
  const out = mapEvent(
    env({
      HERDR_PLUGIN_EVENT: "pane.agent_status_changed",
      HERDR_PANE_ID: "wK:p1",
      HERDR_WORKSPACE_ID: "wK",
      HERDR_PLUGIN_CONTEXT_JSON: JSON.stringify({
        workspace_label: "herdr frontend work",
        focused_pane_agent: "pi",
      }),
    }),
    { host: HOST, info: INFO },
  );
  const s = out.find((e) => e.entity_type === "session");
  assert.equal(s.payload.agent_role, "pi");
});

test("pane.exited emits session.terminated with status stopped", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "pane.exited", HERDR_PANE_ID: "wK:p1" }),
    { host: HOST, info: INFO },
  );
  const s = out.find((e) => e.entity_type === "session");
  assert.equal(s.event_type, "session.terminated");
  assert.equal(s.payload.status, "stopped");
});

// ---------------------------------------------------------------------------
// noise reduction: non-agent panes are NOT tracked
// ---------------------------------------------------------------------------
test("pane.focused does NOT emit a session (avoid the noisy flood)", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "pane.focused", HERDR_PANE_ID: "wK:p2" }),
    { host: HOST, info: INFO },
  );
  assert.ok(!out.some((e) => e.entity_type === "session"));
});

test("pane.created does NOT emit a session (only real agents count)", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "pane.created", HERDR_PANE_ID: "wK:p3" }),
    { host: HOST, info: INFO },
  );
  assert.ok(!out.some((e) => e.entity_type === "session"));
});

// ---------------------------------------------------------------------------
// robustness
// ---------------------------------------------------------------------------
test("unknown event yields no envelopes (never crashes)", () => {
  const out = mapEvent(env({ HERDR_PLUGIN_EVENT: "worktree.opened" }));
  assert.deepEqual(out, []);
});

test("missing HERDR_PLUGIN_EVENT yields no envelopes", () => {
  assert.deepEqual(mapEvent(env({})), []);
});

test("mapEvent is deterministic given the same env + ctx", () => {
  const e = env({
    HERDR_PLUGIN_EVENT: "pane.agent_status_changed",
    HERDR_PANE_ID: "wK:p1",
    HERDR_WORKSPACE_ID: "wK",
    HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ status: "working" }),
  });
  const a = mapEvent(e, { host: HOST, info: INFO });
  const b = mapEvent(e, { host: HOST, info: INFO });
  assert.deepEqual(a, b);
});

// ---------------------------------------------------------------------------
// session.json resolution
// ---------------------------------------------------------------------------
test("workspaceInfo reads the human label + cwd from session.json", () => {
  const dir = mkdtempSync(join(tmpdir(), "cp-feed-"));
  const ws = { id: "wK", custom_name: "herdr frontend work",
    identity_cwd: "/home/wohlgemuth/IdeaProjects" };
  writeFileSync(join(dir, "session.json"),
    JSON.stringify({ workspaces: [ws] }));
  const env2 = { HERDR_SESSION_JSON: join(dir, "session.json") };
  assert.deepEqual(workspaceInfo(env2, "wK"), {
    title: "herdr frontend work",
    cwd: "/home/wohlgemuth/IdeaProjects",
  });
  // unknown id falls back to the id
  assert.deepEqual(workspaceInfo(env2, "wZ"), { title: "wZ", cwd: undefined });
});

test("sessionJsonPath is found via HERDR_SESSION_JSON", () => {
  const dir = mkdtempSync(join(tmpdir(), "cp-feed-"));
  writeFileSync(join(dir, "session.json"), "{}");
  const p = sessionJsonPath({ HERDR_SESSION_JSON: join(dir, "session.json") });
  assert.equal(p, join(dir, "session.json"));
});

test("sessionJsonPath is derived from the plugin config dir", () => {
  // <herdr-config>/plugins/config/herdr.cp-feed -> up 3 = <herdr-config>
  const dir = mkdtempSync(join(tmpdir(), "cp-feed-"));
  const cfg = join(dir, "plugins", "config", "herdr.cp-feed");
  writeFileSync(join(dir, "session.json"), "{}");
  const p = sessionJsonPath({ HERDR_PLUGIN_CONFIG_DIR: cfg });
  assert.equal(p, join(dir, "session.json"));
});

// ---------------------------------------------------------------------------
// URL resolution
// ---------------------------------------------------------------------------
test("resolveUrl reads host.json, not env", () => {
  const home = mkdtempSync(join(tmpdir(), "cp-home-"));
  mkdirSync(join(home, ".config", "herdr-engineering"), { recursive: true });
  writeFileSync(join(home, ".config", "herdr-engineering", "host.json"),
    JSON.stringify({ control_plane_url: "http://cp.example/", host_id: "h", token: "t" }));
  assert.equal(resolveUrl(env({ HOME: home, HERDR_CP_URL: "http://ignored.test" })), "http://cp.example");
});

test("resolveUrl is null when the host has not joined", () => {
  const home = mkdtempSync(join(tmpdir(), "cp-home-"));
  assert.equal(resolveUrl(env({ HOME: home, HERDR_CP_URL: "http://ignored.test" })), null);
});

test("sessions on different hosts and named servers cannot collide", () => {
  const base = { HERDR_PLUGIN_EVENT: "pane.agent_detected", HERDR_PANE_ID: "w1:p1", HERDR_SESSION_NAME: "eng" };
  const a = mapEvent(base, { host: "bender" }).find(e => e.entity_type === "session");
  const b = mapEvent(base, { host: "beast" }).find(e => e.entity_type === "session");
  const c = mapEvent({ ...base, HERDR_SESSION_NAME: "other" }, { host: "beast" }).find(e => e.entity_type === "session");
  assert.equal(a.entity_id, "herdr:bender:eng:w1%3Ap1");
  assert.notEqual(a.entity_id, b.entity_id);
  assert.notEqual(b.entity_id, c.entity_id);
  assert.equal(b.payload.pane_id, "w1:p1");
  assert.equal(b.payload.herdr_session, "eng");
});
