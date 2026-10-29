import { test } from "node:test";
import assert from "node:assert/strict";
import { createReportWorker, runReport } from "./report-worker.js";

const target = { url: "http://cp", apiKey: "k" };
const job = { operation_id: "op1", session_id: "s1", agent: "claude", target: "abc-123", harness: "claude", timeout_seconds: 300 };
const noSleep = () => Promise.resolve();

function fakeFetch(handlers) {
  const calls = [];
  const fn = async (url, init) => {
    const path = url.split("/summary-reports/")[1];
    const body = JSON.parse(init.body);
    calls.push({ path, body, headers: init.headers, init });
    const [status, json] = handlers[path] ? await handlers[path](body, calls.filter(c => c.path === path).length) : [200, {}];
    return { ok: status < 300, status, json: async () => json, body: { cancel: async () => {} } };
  };
  fn.calls = calls;
  return fn;
}

test("claims for its own host, runs the job, and posts the result", async () => {
  const fetcher = fakeFetch({ claim: () => [200, { jobs: [job] }] });
  const ran = [];
  const tick = createReportWorker({ fetcher, hostId: "bender", sleep: noSleep,
    run: async j => { ran.push(j); return { ok: true, report: "# Session summary\nEvidence coverage: x", harness: "claude", duration_s: 3 }; } });
  assert.deepEqual(await tick(target), { ran: true, ok: true });
  assert.deepEqual(ran, [job]);
  const [claim, result] = fetcher.calls;
  assert.deepEqual(claim.body, { host_id: "bender" });
  assert.equal(claim.headers["X-API-Key"], "k");
  assert.equal(result.path, "result");
  assert.deepEqual(result.body, { ok: true, report: "# Session summary\nEvidence coverage: x", harness: "claude", duration_s: 3, host_id: "bender", session_id: "s1", operation_id: "op1" });
  assert.equal(result.init.redirect, "error");
});

test("nothing to do, refusals and network errors are quiet", async () => {
  const tick = createReportWorker({ fetcher: fakeFetch({ claim: () => [200, { jobs: [] }] }), hostId: "bender", run: async () => assert.fail("no job") });
  assert.equal((await tick(target)).ran, false);
  assert.equal((await createReportWorker({ fetcher: fakeFetch({ claim: () => [403, {}] }), hostId: "b" })(target)).status, 403);
  assert.equal((await createReportWorker({ fetcher: async () => { throw new Error("down"); }, hostId: "b" })(target)).ran, false);
  assert.equal((await createReportWorker({ fetcher: fakeFetch({}), hostId: "" })(target)).ran, false);
  assert.equal((await createReportWorker({ fetcher: fakeFetch({}), hostId: "b" })({})).ran, false);
});

test("one job at a time: a tick during a run does not claim again", async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const fetcher = fakeFetch({ claim: () => [200, { jobs: [job] }] });
  const tick = createReportWorker({ fetcher, hostId: "bender", sleep: noSleep, run: async () => { await gate; return { ok: true, report: "r" }; } });
  const first = tick(target);
  await new Promise(r => setImmediate(r));
  assert.equal((await tick(target)).ran, false);
  assert.equal(fetcher.calls.filter(c => c.path === "claim").length, 1);
  release();
  assert.equal((await first).ran, true);
});

test("failures are reported with their code, and a crashing run still answers", async () => {
  const fetcher = fakeFetch({ claim: () => [200, { jobs: [job] }] });
  const tick = createReportWorker({ fetcher, hostId: "h", sleep: noSleep, run: async () => ({ ok: false, error: "timeout", detail: "x".repeat(900) }) });
  assert.deepEqual(await tick(target), { ran: true, ok: false });
  const result = fetcher.calls.find(c => c.path === "result").body;
  assert.equal(result.error, "timeout");
  assert.equal(result.detail.length, 300);
  const crash = fakeFetch({ claim: () => [200, { jobs: [job] }] });
  await createReportWorker({ fetcher: crash, hostId: "h", sleep: noSleep, run: async () => { throw new Error("boom"); } })(target);
  assert.equal(crash.calls.find(c => c.path === "result").body.error, "summarizer_failed");
});

test("a result post is retried on server errors and dropped on a refusal", async () => {
  const flaky = fakeFetch({ claim: () => [200, { jobs: [job] }], result: (_b, n) => [n < 3 ? 503 : 200, {}] });
  const out = await createReportWorker({ fetcher: flaky, hostId: "h", sleep: noSleep, run: async () => ({ ok: true, report: "r" }) })(target);
  assert.equal(out.ok, true);
  assert.equal(flaky.calls.filter(c => c.path === "result").length, 3);
  const refused = fakeFetch({ claim: () => [200, { jobs: [job] }], result: () => [403, {}] });
  const r = await createReportWorker({ fetcher: refused, hostId: "h", sleep: noSleep, run: async () => ({ ok: true, report: "r" }) })(target);
  assert.equal(r.rejected, 403);
  assert.equal(refused.calls.filter(c => c.path === "result").length, 1);
  const dead = fakeFetch({ claim: () => [200, { jobs: [job] }], result: () => [503, {}] });
  assert.equal((await createReportWorker({ fetcher: dead, hostId: "h", sleep: noSleep, run: async () => ({ ok: true, report: "r" }) })(target)).undelivered, true);
});

test("runReport uses fixed argv with no shell and refuses anything else", async () => {
  let seen;
  const exec = (cmd, args, opts, cb) => { seen = { cmd, args, opts }; cb(null, JSON.stringify({ ok: true, report: "r" })); };
  assert.deepEqual(await runReport(job, { execFileFn: exec, env: { HOME: "/h", PATH: "/usr/bin" } }), { ok: true, report: "r" });
  assert.equal(seen.cmd, "herdr-eng");
  assert.deepEqual(seen.args, ["summary", "report", "--agent", "claude", "--session", "abc-123", "--harness", "claude", "--timeout", "300"]);
  assert.match(seen.opts.env.PATH, /^\/h\/\.local\/bin:/);
  assert.equal(seen.opts.shell, undefined);
  const never = () => assert.fail("must not run");
  for (const bad of [{ ...job, target: "--resume" }, { ...job, target: "a b" }, { ...job, agent: "gemini" }, { ...job, harness: "codex" }, { ...job, target: 5 }]) {
    assert.deepEqual(await runReport(bad, { execFileFn: never }), { ok: false, error: "invalid_session_id" });
  }
});

test("runReport maps process failures to stable codes", async () => {
  const run = (err, out = "") => runReport(job, { execFileFn: (c, a, o, cb) => cb(err, out) });
  assert.deepEqual(await run(Object.assign(new Error("x"), { killed: true })), { ok: false, error: "timeout" });
  assert.deepEqual(await run(Object.assign(new Error("x"), { code: "ENOENT" })), { ok: false, error: "harness_missing" });
  assert.deepEqual(await run(new Error("x"), "not json"), { ok: false, error: "summarizer_failed" });
  assert.deepEqual(await run(Object.assign(new Error("exit 1"), { code: 1 }), JSON.stringify({ ok: false, error: "no_session_record", detail: "none" })), { ok: false, error: "no_session_record", detail: "none" });
});
