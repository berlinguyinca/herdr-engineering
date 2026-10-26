// Unit tests for the plugin's live session enrichment (enrich.js).
//
// Run with:  node --test enrich.test.js
//
// The pure parts (herdrBin, mergeDetail, attachCommand) are fully tested here.
// The subprocess-fetch helpers (paneDetail/processDetail/branchDetail) are
// covered via mergeDetail's contract and a live smoke test on the host.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, chmodSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { herdrBin, mergeDetail, attachCommand, enrichSession } from "./enrich.js";

// ---------------------------------------------------------------------------
// herdrBin
// ---------------------------------------------------------------------------
test("herdrBin prefers explicit HERDR_BIN", () => {
  assert.equal(herdrBin({ HERDR_BIN: "/opt/herdr/bin/herdr" }), "/opt/herdr/bin/herdr");
});

test("herdrBin resolves an executable from PATH", () => {
  const dir = mkdtempSync(join(tmpdir(), "herdr-enrich-"));
  const fake = join(dir, "herdr");
  writeFileSync(fake, "#!/bin/sh\n");
  chmodSync(fake, 0o755);
  const bin = herdrBin({ PATH: dir });
  assert.equal(bin, fake);
});

test("herdrBin falls back to ~/.local/bin/herdr", () => {
  const bin = herdrBin({ PATH: "/nonexistent" });
  assert.ok(bin.endsWith(join(".local", "bin", "herdr")));
});

// ---------------------------------------------------------------------------
// mergeDetail
// ---------------------------------------------------------------------------
function sessionEnvelope(overrides = {}) {
  return {
    entity_type: "session",
    entity_id: "w1H:p5",
    event_type: "session.updated",
    payload: { host_id: "bender", mission_id: "w1H", agent_role: "pi", status: "active", ...overrides },
  };
}

test("mergeDetail adds non-empty detail fields to a session payload", () => {
  const e = sessionEnvelope();
  const out = mergeDetail(e, {
    agent_role: "claude", cwd: "/work", branch: "main@abc123",
    pid: "1234", cmdline: "pi --x", summary: "fixing bug", attach: `ssh -t beast "bash -lc 'herdr --session default agent attach w1:p1'"`,
  });
  assert.equal(out.payload.cwd, "/work");
  assert.equal(out.payload.branch, "main@abc123");
  assert.equal(out.payload.pid, "1234");
  assert.equal(out.payload.cmdline, "pi --x");
  assert.equal(out.payload.summary, "fixing bug");
  assert.equal(out.payload.attach, `ssh -t beast "bash -lc 'herdr --session default agent attach w1:p1'"`);
  // agent_role from detail updates when empty, but existing identity wins
  assert.equal(out.payload.agent_role, "pi");
});

test("mergeDetail fills an empty agent_role from detail", () => {
  const e = sessionEnvelope({ agent_role: "" });
  const out = mergeDetail(e, { agent_role: "claude" });
  assert.equal(out.payload.agent_role, "claude");
});

test("mergeDetail ignores null/undefined/empty detail fields", () => {
  const e = sessionEnvelope();
  const out = mergeDetail(e, { cwd: null, branch: undefined, pid: "", summary: null, attach: "" });
  assert.equal(out.payload.cwd, undefined);
  assert.equal(out.payload.branch, undefined);
  assert.equal(out.payload.pid, undefined);
});

test("mergeDetail leaves non-session envelopes untouched", () => {
  const m = { entity_type: "mission", entity_id: "w1H", payload: { title: "t" } };
  const out = mergeDetail(m, { cwd: "/x" });
  assert.equal(out, m);
  assert.equal(out.payload.cwd, undefined);
});

test("mergeDetail handles a missing envelope or detail gracefully", () => {
  assert.equal(mergeDetail(null, { cwd: "/x" }), null);
  const e = sessionEnvelope();
  assert.equal(mergeDetail(e, null), e);
});

// ---------------------------------------------------------------------------
// attachCommand
// ---------------------------------------------------------------------------
// Same form as herdr_engineering/control_plane/terminal_attach.py's "remote" command: one
// ssh invocation whose remote side runs herdr. Never "ssh host && ..." (the && half would
// run on the viewer's machine, not on the host).
test("attachCommand builds the single ssh command terminal_attach uses", () => {
  assert.equal(attachCommand("beast", "default", "w1:p2"),
    `ssh -t beast "bash -lc 'herdr --session default agent attach w1:p2'"`);
});

test("attachCommand never chains a local command after ssh", () => {
  const cmd = attachCommand("beast", "eng", "wG");
  assert.ok(!cmd.includes("&&"));
  assert.ok(!cmd.includes("workspace focus"));
});

test("attachCommand refuses unsafe or missing identity", () => {
  assert.equal(attachCommand("beast; rm -rf ~", "default", "w1:p2"), null);
  assert.equal(attachCommand("beast", "de'fault", "w1:p2"), null);
  assert.equal(attachCommand("beast", "default", "w1 p2"), null);
  assert.equal(attachCommand(undefined, "default", "w1:p2"), null);
  assert.equal(attachCommand("beast", null, "w1:p2"), null);
  assert.equal(attachCommand("beast", "default", ""), null);
});

test("namespaced telemetry queries its native pane on the named server", async () => {
  const { enrichSession } = await import("./enrich.js");
  const { readFileSync, rmSync } = await import("node:fs");
  const dir = mkdtempSync(join(tmpdir(), "herdr-native-"));
  const binary = join(dir, "herdr.mjs");
  const log = join(dir, "args.jsonl");
  writeFileSync(binary, `#!/usr/bin/env node
import {appendFileSync} from 'node:fs';
appendFileSync(${JSON.stringify(log)}, JSON.stringify(process.argv.slice(2))+'\\n');
console.log(JSON.stringify({ok:true,result:{pane:{terminal_id:'terminal-1',revision:5,agent_session:{source:'integration',agent:'pi',kind:'path',value:'/tmp/conversation.jsonl'}}}}));
`);
  chmodSync(binary, 0o755);
  try {
    const envelope = { entity_type: "session", entity_id: "herdr:beast:eng:w1%3Ap1", source: "herdr", payload: { pane_id: "w1:p1", host_id: "beast", herdr_session: "eng", mission_id: "herdr-ws:w1" } };
    const result = await enrichSession({ HERDR_BIN: binary, HERDR_SESSION_NAME: "eng" }, envelope);
    const args = readFileSync(log, "utf8").trim().split("\n").map(JSON.parse);
    assert.ok(args.some(a => JSON.stringify(a) === JSON.stringify(["--session", "eng", "pane", "get", "w1:p1"])));
    assert.ok(args.every(a => !a.includes(envelope.entity_id)));
    assert.equal(result.source, "herdr");
    assert.equal(result.payload.mission_id, "herdr-ws:w1");
    assert.equal(result.payload.workspace_id, "w1");
    assert.equal(result.payload.terminal_id, "terminal-1");
    assert.equal(result.payload.agent_session.value, "/tmp/conversation.jsonl");
    assert.equal(result.payload.revision, 5);
    assert.equal(result.payload.attach, `ssh -t beast "bash -lc 'herdr --session eng agent attach w1:p1'"`);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

for (const [missionId, workspaceId] of [["herdr-ws:w1", "w1"], ["w1", "w1"], [undefined, undefined]]) {
  test(`enrichment preserves workspace identity when CLI is unavailable (${missionId})`, async (t) => {
    const dir = mkdtempSync(join(tmpdir(), "herdr-unavailable-"));
    t.after(() => rmSync(dir, { recursive: true, force: true }));
    const envelope = { source: "herdr", entity_type: "session", entity_id: "herdr:beast:eng:w1%3Ap1",
      payload: { pane_id: "w1:p1", host_id: "beast", herdr_session: "eng", mission_id: missionId } };
    const result = await enrichSession({ HERDR_BIN: join(dir, "missing-herdr") }, envelope);
    assert.equal(result.payload.workspace_id, workspaceId);
    assert.equal(result.payload.mission_id, missionId);
    assert.equal(result.source, "herdr");
    assert.equal(result.payload.cwd, undefined);
    assert.equal(result.payload.attach, `ssh -t beast "bash -lc 'herdr --session eng agent attach w1:p1'"`);
    assert.equal(envelope.payload.workspace_id, undefined);
  });
}

test("namespaced mission does not enable attach without a known server", async () => {
  const envelope = { entity_type: "session", entity_id: "herdr:beast:unknown:w1%3Ap1",
    payload: { pane_id: "w1:p1", host_id: "beast", mission_id: "herdr-ws:w1" } };
  assert.equal(await enrichSession({}, envelope), envelope);
  assert.equal(envelope.payload.attach, undefined);
});
