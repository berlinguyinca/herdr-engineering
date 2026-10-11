// Unit tests for humanize.js (pure, node-testable).
// Run with:  node --test humanize.test.js
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  describeEvent,
  missionSubtitle,
  sessionSummary,
} from "./humanize.js";

const TITLES = { wK: "herdr frontend work", w1H: "wcmc - release" };
const ctx = { missionTitles: TITLES };

test("host registered reads as a human sentence with tailnet ip", () => {
  const d = describeEvent({
    event_type: "HostRegistered",
    entity_id: "bender",
    payload: { host_name: "bender", tailnet_ip: "100.104.39.6" },
  }, ctx);
  assert.equal(d.title, "Host online");
  assert.match(d.detail, /bender joined the fabric \(100\.104\.39\.6\)/);
  assert.equal(d.muted, undefined);
});

test("host heartbeat is muted (low signal)", () => {
  const d = describeEvent({
    event_type: "HostHeartbeat",
    payload: { host_name: "bender" },
  }, ctx);
  assert.equal(d.muted, true);
});

test("mission started resolves the cryptic id to its human title", () => {
  const d = describeEvent({
    event_type: "MissionStarted",
    entity_id: "wK",
    payload: { title: "herdr frontend work", stage: "active" },
  }, ctx);
  assert.match(d.detail, /“herdr frontend work” began/);
  assert.ok(!/wK/.test(d.detail));
});

test("mission updated shows the stage change", () => {
  const d = describeEvent({
    event_type: "MissionUpdated",
    payload: { title: "wcmc - release", stage: "active" },
  }, ctx);
  assert.match(d.detail, /→ active/);
});

test("session started names the agent, mission and host", () => {
  const d = describeEvent({
    event_type: "SessionStarted",
    payload: { agent_role: "pi", mission_id: "wK", host_id: "bender" },
  }, ctx);
  assert.equal(d.title, "pi started");
  assert.match(d.detail, /pi began working in “herdr frontend work” on bender/);
});

test("session updated reports the status with the mission title", () => {
  const d = describeEvent({
    event_type: "SessionUpdated",
    payload: { agent_role: "pi", status: "blocked", mission_id: "wK" },
  }, ctx);
  assert.equal(d.title, "pi → blocked");
  assert.match(d.detail, /pi is blocked in “herdr frontend work”/);
});

test("session terminated says stopped", () => {
  const d = describeEvent({
    event_type: "SessionTerminated",
    payload: { agent_role: "pi", mission_id: "w1H" },
  }, ctx);
  assert.equal(d.title, "pi stopped");
  assert.match(d.detail, /wcmc - release/);
});

test("agent role falls back to 'agent' when absent", () => {
  const d = describeEvent({
    event_type: "SessionStarted",
    payload: { mission_id: "wK" },
  }, ctx);
  assert.equal(d.title, "agent started");
});

test("unknown event falls back to a compact payload summary, no raw json", () => {
  const d = describeEvent({
    event_type: "MysteryThing",
    payload: { alpha: "1", beta: null, nested: { x: 2 } },
  }, ctx);
  assert.equal(d.title, "MysteryThing");
  assert.ok(!/{"alpha"/.test(d.detail));
  assert.match(d.detail, /alpha: 1/);
  assert.ok(!/nested/.test(d.detail)); // object values skipped
});

test("missionSubtitle shows scope and session count", () => {
  assert.equal(missionSubtitle({ purpose: "Ship the dashboard", cwd: "/x" }, 3),
    "Ship the dashboard · 3 session(s)");
  assert.equal(missionSubtitle({}, 0), "—");
});

test("sessionSummary hides zero message/tool counts (uninformative)", () => {
  const s = { agent_role: "pi", model: "gpt5", mission_id: "wK",
    host_id: "bender", messages: 0, tool_calls: 0 };
  const summary = sessionSummary(s, TITLES);
  assert.match(summary, /pi · gpt5 · in “herdr frontend work” on bender/);
  assert.ok(!/0 msg/.test(summary));
  assert.ok(!/tool calls/.test(summary));
});

test("sessionSummary includes message counts only when non-zero", () => {
  const s = { agent_role: "pi", messages: 12, tool_calls: 4 };
  const summary = sessionSummary(s, TITLES);
  assert.match(summary, /12 msg \/ 4 tool calls/);
});
