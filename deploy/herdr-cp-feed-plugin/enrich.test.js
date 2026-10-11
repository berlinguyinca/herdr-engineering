// Unit tests for the plugin's live session enrichment (enrich.js).
//
// Run with:  node --test enrich.test.js
//
// The pure parts (herdrBin, mergeDetail, attachCommand) are fully tested here.
// The subprocess-fetch helpers (paneDetail/processDetail/branchDetail) are
// covered via mergeDetail's contract and a live smoke test on the host.
import { test } from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import { mkdtempSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { herdrBin, mergeDetail, attachCommand } from "./enrich.js";

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
    pid: "1234", cmdline: "pi --x", summary: "fixing bug", attach: "herdr workspace focus w1H",
  });
  assert.equal(out.payload.cwd, "/work");
  assert.equal(out.payload.branch, "main@abc123");
  assert.equal(out.payload.pid, "1234");
  assert.equal(out.payload.cmdline, "pi --x");
  assert.equal(out.payload.summary, "fixing bug");
  assert.equal(out.payload.attach, "herdr workspace focus w1H");
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
test("attachCommand is host-local when host matches os.hostname", () => {
  assert.equal(attachCommand({ HOSTNAME: os.hostname() }, os.hostname(), "w1H"),
    "herdr workspace focus w1H");
});

test("attachCommand prefixes ssh for a remote host", () => {
  const cmd = attachCommand({ HOSTNAME: os.hostname() }, "beast", "wG");
  assert.ok(cmd.startsWith("ssh beast && "));
  assert.ok(cmd.endsWith("herdr workspace focus wG"));
});
