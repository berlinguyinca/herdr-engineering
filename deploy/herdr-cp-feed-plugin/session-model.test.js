import test from "node:test";
import assert from "node:assert/strict";
import { piModel, claudeModel, sessionModel } from "./session-model.js";

const pi = [
  '{"type":"session","id":"x"}',
  '{"type":"model_change","provider":"metabolomics","modelId":"qwen-a"}',
  '{"type":"message","text":"model_change in prose"}',
  '{"type":"model_change","provider":"development","modelId":"qwen-b"}',
].join("\n");

test("pi: the last model_change wins and carries its provider", () => {
  assert.equal(piModel(pi), "development/qwen-b");
  assert.equal(piModel('{"type":"session"}'), null);
});

test("claude: last real assistant model, synthetic ignored", () => {
  const text = ['{"message":{"model":"claude-opus-5-5"}}', '{"message":{"model":"<synthetic>"}}', "{broken"].join("\n");
  assert.equal(claudeModel(text), "claude-opus-5-5");
});

test("session paths outside the pi sessions dir are never read", () => {
  const read = () => { throw new Error("must not read"); };
  assert.equal(sessionModel("pi", { kind: "path", value: "/etc/passwd.jsonl" }, { home: "/home/u", read }), null);
  assert.equal(sessionModel("pi", { kind: "path", value: "/home/u/.pi/agent/sessions/../../auth.jsonl" }, { home: "/home/u", read }), null);
  assert.equal(sessionModel("pi", { kind: "path", value: "/home/u/.pi/agent/sessions/p/a.jsonl" }, { home: "/home/u", read: () => pi }), "development/qwen-b");
  assert.equal(sessionModel("claude", { kind: "id", value: "../../x" }, { home: "/home/u", read }), null);
});

import { createModelResolver, controlDir } from "./session-model.js";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("pi pane resolves through pid -> descriptor -> session log, and is cached", async () => {
  const home = mkdtempSync(join(tmpdir(), "m-"));
  const ctl = join(home, "ctl");
  mkdirSync(ctl);
  writeFileSync(join(ctl, "a.json"), JSON.stringify({ pid: 4242, sessionId: "01a14589-160f-71a9" }));
  writeFileSync(join(ctl, "junk.json"), "not json");
  const dir = join(home, ".pi", "agent", "sessions", "--p--");
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "2026-10-16T16_01a14589-160f-71a9.jsonl"),
    '{"type":"model_change","provider":"metabolomics","modelId":"qwen-x"}\n');
  let calls = 0, t = 0;
  const resolve = createModelResolver({ env: { PI_ENGINEERING_CONTROL_DIR: ctl }, home, now: () => t,
    queryProcess: async () => { calls++; return { result: { process_info: { foreground_processes: [{ pid: 4242 }] } } }; } });
  const pane = { agent: "pi", pane_id: "w:p1", terminal_id: "term_1" };
  assert.equal(await resolve("default", pane), "metabolomics/qwen-x");
  assert.equal(await resolve("default", pane), "metabolomics/qwen-x");
  assert.equal(calls, 1);
  t = 400_000;
  await resolve("default", pane);
  assert.equal(calls, 2);
  assert.equal(await resolve("default", { agent: "pi", pane_id: "w:p2", terminal_id: "term_2" }) , "metabolomics/qwen-x");
});

test("control dir honours override then XDG_RUNTIME_DIR", () => {
  assert.equal(controlDir({ PI_ENGINEERING_CONTROL_DIR: "/x" }), "/x");
  assert.equal(controlDir({ XDG_RUNTIME_DIR: "/run/user/1" }), "/run/user/1/pi-engineering");
});
