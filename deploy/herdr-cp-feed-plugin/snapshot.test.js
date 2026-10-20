import { test } from "node:test";
import assert from "node:assert/strict";
import { collectSnapshot, createSnapshotPublisher, publishSnapshot } from "./snapshot.js";

const server = name => ({ name, running: true, socket_path: `/sessions/${name}/herdr.sock` });
const pane = (index = 0) => ({ pane_id: `w1:p${index}`, terminal_id: `term_${index}`, agent: "pi", cwd: "/work/real", agent_status: "idle", revision: 5 });

function reader(sessions, agents, calls = []) {
  return async (_binary, args) => {
    calls.push(args);
    return args[0] === "session" ? { sessions } : { result: { snapshot: { agents } } };
  };
}

test("snapshot uses explicit registry server and native identities, preserves terminal outcomes", async () => {
  const calls = [], directories = [];
  const result = await collectSnapshot({ HERDR_BIN: "/fixture/herdr", HOSTNAME: "beast" }, {
    queryNative: reader([server("eng")], [{ ...pane(), agent_status: "failed", foreground_cwd: "/work/foreground" }], calls),
    metadata: async cwd => { directories.push(cwd); return { repository_id: "github.com/team/app", commit_sha: "a".repeat(40) }; },
  });
  assert.deepEqual(calls, [["session", "list", "--json"], ["--session", "eng", "api", "snapshot"]]);
  assert.deepEqual(directories, ["/work/foreground"]);
  assert.equal(result.envelopes[0].entity_id, "herdr:beast:eng:w1%3Ap0");
  assert.equal(result.envelopes[0].payload.status, "failed");
  assert.equal(result.envelopes[0].payload.revision, 5);
  assert.equal(result.envelopes[0].payload.agent_session, undefined);
  assert.equal(result.envelopes[0].payload.interactive_ready, undefined);
  assert.equal(result.envelopes[0].payload.mission_id, undefined);
  assert.deepEqual(result.summary, { servers: 1, sessions: 1, linked: 1, skipped: 0, errors: 0, truncated: false });
});

test("ambiguous servers and unverified or duplicate terminals never create session records", async () => {
  const common = { metadata: async () => ({}) };
  const ambiguous = await collectSnapshot({}, { ...common, queryNative: reader([server("same"), server("same")], [pane()]) });
  assert.equal(ambiguous.envelopes.length, 0);
  assert.equal(ambiguous.summary.errors, 2);
  const result = await collectSnapshot({}, { ...common, queryNative: reader([server("eng")], [pane(), pane(), { ...pane(1), terminal_id: null }]) });
  assert.equal(result.envelopes.length, 0);
  assert.equal(result.summary.skipped, 3);
});

test("server and pane bounds report truncation rather than silently claiming full inventory", async () => {
  const result = await collectSnapshot({}, {
    queryNative: reader(Array.from({ length: 10 }, (_, n) => server(`s${n}`)), Array.from({ length: 140 }, (_, n) => pane(n))),
    metadata: async () => ({}),
  });
  assert.equal(result.envelopes.length, 128);
  assert.equal(result.summary.servers, 8);
  assert.equal(result.summary.truncated, true);
});

test("elapsed collection budget reports partial inventory", async () => {
  let stamp = 0;
  const result = await collectSnapshot({}, {
    queryNative: reader([server("eng")], [pane(), pane(1)]), now: () => stamp,
    metadata: async () => { stamp += 20001; return {}; },
  });
  assert.equal(result.envelopes.length, 1);
  assert.equal(result.summary.truncated, true);
});

test("missing native registry reports failure without inventing stopped sessions", async () => {
  const result = await collectSnapshot({}, { queryNative: async () => null });
  assert.deepEqual(result.envelopes, []);
  assert.equal(result.summary.errors, 1);
});

test("publication uses private key header and stops on first rejected write", async () => {
  const calls = [];
  const result = await publishSnapshot({ HERDR_CP_URL: "http://control.test", HERDR_CP_API_KEY: "fixture-key" }, [{ n: 1 }, { n: 2 }], async (url, options) => {
    calls.push({ url, options });
    return { ok: false, body: { cancel: async () => {} } };
  });
  assert.deepEqual(result, { posted: 0, unchanged: 0, errors: 1 });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://control.test/api/v1/ingest");
  assert.equal(calls[0].options.headers["X-API-Key"], "fixture-key");
  assert.equal(calls[0].options.redirect, "error");
});

test("publisher deduplicates snapshots, publishes commit changes and heartbeats after five minutes", async () => {
  let stamp = 0;
  const calls = [];
  const publish = createSnapshotPublisher({ now: () => stamp, fetcher: async (_url, options) => {
    calls.push(JSON.parse(options.body)); return { ok: true };
  } });
  const row = { entity_id: "herdr:host:eng:pane", payload: { commit_sha: "a".repeat(40) } };
  assert.equal((await publish({}, [row])).posted, 1);
  assert.equal((await publish({}, [row])).unchanged, 1);
  // Missing observations do not remove or close known sessions.
  await publish({}, []);
  assert.equal((await publish({}, [row])).unchanged, 1);
  row.payload.commit_sha = "b".repeat(40);
  assert.equal((await publish({}, [row])).posted, 1);
  stamp = 299999;
  assert.equal((await publish({}, [row])).unchanged, 1);
  stamp = 300000;
  assert.equal((await publish({}, [row])).posted, 1);
  assert.equal(calls.length, 3);
});

test("publisher bounds remembered records and retries a rejected update", async () => {
  let good = false;
  const publish = createSnapshotPublisher({ maxEntries: 1, fetcher: async () => ({ ok: good }) });
  const a = { entity_id: "a", payload: {} }, b = { entity_id: "b", payload: {} };
  assert.equal((await publish({}, [a])).errors, 1);
  good = true;
  assert.equal((await publish({}, [a])).posted, 1);
  await publish({}, [b]);
  assert.equal((await publish({}, [a])).posted, 1);
});
