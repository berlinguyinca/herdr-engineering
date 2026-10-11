// Unit tests for the HerdR control-plane feed plugin's event→envelope mapping.
//
// Run with:  node --test emit.test.js   (Node 18+; global fetch not needed here)
import { test } from "node:test";
import assert from "node:assert/strict";
import { mapEvent, resolveUrl, CONTROL_PLANE_DEFAULT } from "./emit.js";

const HOST = "bender";

function env(overrides = {}) {
  return { HOSTNAME: HOST, ...overrides };
}

// ---------------------------------------------------------------------------
// host
// ---------------------------------------------------------------------------
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

test("workspace.focused emits host.heartbeat + mission.updated", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "workspace.focused", HERDR_WORKSPACE_ID: "w1" }),
  );
  const types = out.map((e) => e.event_type);
  assert.ok(types.includes("host.heartbeat"));
  assert.ok(types.includes("mission.updated"));
  const hb = out.find((e) => e.event_type === "host.heartbeat");
  assert.deepEqual(hb.payload, { host_name: HOST });
});

// ---------------------------------------------------------------------------
// mission (workspace)
// ---------------------------------------------------------------------------
test("workspace.created emits mission.started bound to workspace id", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "workspace.created", HERDR_WORKSPACE_ID: "w1" }),
  );
  const m = out.find((e) => e.entity_type === "mission");
  assert.ok(m);
  assert.equal(m.entity_id, "w1");
  assert.equal(m.event_type, "mission.started");
  assert.equal(m.payload.title, "w1");
  assert.equal(m.payload.stage, "active");
});

test("workspace.renamed updates the mission title", () => {
  const out = mapEvent(
    env({
      HERDR_PLUGIN_EVENT: "workspace.renamed",
      HERDR_WORKSPACE_ID: "w1",
      HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ name: "HerdR Dev Fabric" }),
    }),
  );
  const m = out.find((e) => e.entity_type === "mission");
  assert.ok(m);
  assert.equal(m.event_type, "mission.updated");
  assert.equal(m.payload.title, "HerdR Dev Fabric");
});

test("workspace.closed emits mission.ended with stage complete", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "workspace.closed", HERDR_WORKSPACE_ID: "w1" }),
  );
  const m = out.find((e) => e.entity_type === "mission");
  assert.ok(m);
  assert.equal(m.event_type, "mission.ended");
  assert.equal(m.payload.stage, "complete");
});

// ---------------------------------------------------------------------------
// session (pane / agent)
// ---------------------------------------------------------------------------
test("pane.agent_detected emits session.started linked to host + mission", () => {
  const out = mapEvent(
    env({
      HERDR_PLUGIN_EVENT: "pane.agent_detected",
      HERDR_WORKSPACE_ID: "w1",
      HERDR_PANE_ID: "w1:p1",
      HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ agent_kind: "pi" }),
    }),
  );
  const s = out.find((e) => e.entity_type === "session");
  assert.ok(s);
  assert.equal(s.entity_id, "w1:p1");
  assert.equal(s.event_type, "session.started");
  assert.equal(s.payload.host_id, HOST);
  assert.equal(s.payload.mission_id, "w1");
  assert.equal(s.payload.agent_role, "pi");
  assert.equal(s.payload.status, "active");
});

test("pane.created also emits session.started when no agent kind yet", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "pane.created", HERDR_PANE_ID: "w1:p1" }),
  );
  const s = out.find((e) => e.entity_type === "session");
  assert.ok(s);
  assert.equal(s.event_type, "session.started");
  assert.equal(s.payload.agent_role, "");
});

test("pane.agent_status_changed emits session.updated with status", () => {
  const out = mapEvent(
    env({
      HERDR_PLUGIN_EVENT: "pane.agent_status_changed",
      HERDR_PANE_ID: "w1:p1",
      HERDR_WORKSPACE_ID: "w1",
      HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ status: "blocked" }),
    }),
  );
  const s = out.find((e) => e.entity_type === "session");
  assert.ok(s);
  assert.equal(s.event_type, "session.updated");
  assert.equal(s.payload.status, "blocked");
});

test("pane.exited emits session.terminated with status stopped", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "pane.exited", HERDR_PANE_ID: "w1:p1" }),
  );
  const s = out.find((e) => e.entity_type === "session");
  assert.ok(s);
  assert.equal(s.event_type, "session.terminated");
  assert.equal(s.payload.status, "stopped");
});

test("pane.closed emits session.terminated too", () => {
  const out = mapEvent(
    env({ HERDR_PLUGIN_EVENT: "pane.closed", HERDR_PANE_ID: "w1:p1" }),
  );
  assert.ok(out.some((e) => e.event_type === "session.terminated"));
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

test("mapEvent is deterministic given the same env", () => {
  const e = env({
    HERDR_PLUGIN_EVENT: "pane.agent_status_changed",
    HERDR_PANE_ID: "w1:p1",
    HERDR_WORKSPACE_ID: "w1",
    HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ status: "working" }),
  });
  assert.deepEqual(mapEvent(e), mapEvent(e));
});

// ---------------------------------------------------------------------------
// URL resolution
// ---------------------------------------------------------------------------
test("resolveUrl defaults to the dev.lan control plane", () => {
  assert.equal(resolveUrl(env({})), CONTROL_PLANE_DEFAULT);
});

test("resolveUrl honors HERDR_CP_URL env", () => {
  assert.equal(
    resolveUrl(env({ HERDR_CP_URL: "http://100.104.39.6" })),
    "http://100.104.39.6",
  );
});
