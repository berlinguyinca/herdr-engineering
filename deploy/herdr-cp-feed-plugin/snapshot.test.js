import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmodSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { mkdirSync, existsSync } from "node:fs";
import { collectSnapshot, createSnapshotPublisher, publishSnapshot, main } from "./snapshot.js";
import { loadHostConfig } from "./host-config.js";

const collect = (env, options) => collectSnapshot(env, { resolveModel: async () => null, ...options });

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
  const result = await collect({ HERDR_BIN: "/fixture/herdr", HOSTNAME: "beast" }, {
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
  const ambiguous = await collect({}, { ...common, queryNative: reader([server("same"), server("same")], [pane()]) });
  assert.equal(ambiguous.envelopes.length, 0);
  assert.equal(ambiguous.summary.errors, 2);
  const result = await collect({}, { ...common, queryNative: reader([server("eng")], [pane(), pane(), { ...pane(1), terminal_id: null }]) });
  assert.equal(result.envelopes.length, 0);
  assert.equal(result.summary.skipped, 3);
});

test("server and pane bounds report truncation rather than silently claiming full inventory", async () => {
  const result = await collect({}, {
    queryNative: reader(Array.from({ length: 10 }, (_, n) => server(`s${n}`)), Array.from({ length: 140 }, (_, n) => pane(n))),
    metadata: async () => ({}),
  });
  assert.equal(result.envelopes.length, 128);
  assert.equal(result.summary.servers, 8);
  assert.equal(result.summary.truncated, true);
});

test("elapsed collection budget reports partial inventory", async () => {
  let stamp = 0;
  const result = await collect({}, {
    queryNative: reader([server("eng")], [pane(), pane(1)]), now: () => stamp,
    metadata: async () => { stamp += 20001; return {}; },
  });
  assert.equal(result.envelopes.length, 1);
  assert.equal(result.summary.truncated, true);
});

test("missing native registry reports failure without inventing stopped sessions", async () => {
  const result = await collect({}, { queryNative: async () => null });
  assert.deepEqual(result.envelopes, []);
  assert.equal(result.summary.errors, 1);
});

test("publication uses private key header and stops on first rejected write", async () => {
  const calls = [];
  const result = await publishSnapshot({ url: "http://control.test", apiKey: "fixture-key" }, [{ n: 1 }, { n: 2 }], async (url, options) => {
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

test("unchanged terminal sessions never heartbeat but changed terminal evidence still publishes", async () => {
  for (const status of ["finished", "stopped", "failed", " CRASHED ", "completed", "closed"]) {
    let stamp = 0;
    const publish = createSnapshotPublisher({ now: () => stamp, fetcher: async () => ({ ok: true }) });
    const row = { entity_id: "terminal", payload: { status, commit_sha: "a".repeat(40) } };
    assert.equal((await publish({}, [row])).posted, 1);
    stamp = 300001;
    assert.equal((await publish({}, [row])).unchanged, 1);
    stamp = 30 * 86400000;
    assert.equal((await publish({}, [row])).unchanged, 1);
    row.payload.commit_sha = "b".repeat(40);
    assert.equal((await publish({}, [row])).posted, 1);
    row.payload.status = "working";
    assert.equal((await publish({}, [row])).posted, 1);
    stamp += 300000;
    assert.equal((await publish({}, [row])).posted, 1);
  }
});

test("collector entrypoint runs through the deployment current symlink", t => {
  const dir = mkdtempSync(join(tmpdir(), "herdr-snapshot-entry-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const binary = join(dir, "herdr");
  writeFileSync(binary, `#!${process.execPath}\nconsole.log(JSON.stringify({sessions:[]}));\n`);
  chmodSync(binary, 0o755);
  const entry = join(dir, "snapshot.js");
  symlinkSync(fileURLToPath(new URL("./snapshot.js", import.meta.url)), entry);
  const result = execFileSync(process.execPath, [entry, "--once", "--dry-run"], {
    encoding: "utf8", env: { ...process.env, HERDR_BIN: binary }, timeout: 5000,
  });
  const summary = JSON.parse(result);
  assert.equal(summary.sessions, 0);
  assert.equal(summary.errors, 0);
  assert.equal(summary.posted, 0);
  assert.equal(summary.dry_run, true);
});

test("a resolved model is reported on the session payload", async () => {
  const result = await collectSnapshot({}, {
    queryNative: reader([server("eng")], [pane()]),
    metadata: async () => ({}),
    resolveModel: async (serverName, p) => `${serverName}/${p.agent}-model`,
  });
  assert.equal(result.envelopes[0].payload.model, "eng/pi-model");
});

test("main exits 2 with a stderr message when the host has no config", () => {
  const home = mkdtempSync(join(tmpdir(), "snap-home-"));
  const script = fileURLToPath(new URL("./snapshot.js", import.meta.url));
  let failure;
  try { execFileSync(process.execPath, [script, "--once"], { env: { PATH: process.env.PATH, HOME: home }, stdio: "pipe" }); }
  catch (error) { failure = error; }
  assert.equal(failure?.status, 2);
  assert.match(String(failure.stderr), /host config unavailable/);
});

test("the plugin never reads HERDR_CP_URL or HERDR_CP_API_KEY", async () => {
  const { readFileSync } = await import("node:fs");
  for (const f of ["emit.js", "snapshot.js", "host-config.js", "inventory.js", "enrich.js"]) {
    assert.doesNotMatch(readFileSync(new URL(`./${f}`, import.meta.url), "utf8"), /HERDR_CP_(URL|API_KEY)/, f);
  }
});

// ---- --loop host-config refresh ------------------------------------------
function loopHarness({ passes, fleet, ingest }) {
  const home = mkdtempSync(join(tmpdir(), "loop-home-"));
  mkdirSync(join(home, ".config", "herdr-engineering"), { recursive: true });
  writeFileSync(join(home, ".config", "herdr-engineering", "host.json"),
    JSON.stringify({ control_plane_url: "http://cp.test", host_id: "h1", token: "tok" }));
  let clock = 0, pass = 0, fleetCalls = 0;
  const keysUsed = [];
  const fleetFetch = async (url, init) => {
    if (url.endsWith("/applied")) return { status: 200, text: async () => "{}" };
    fleetCalls++;
    const r = fleet(fleetCalls);
    return { status: r.status, text: async () => JSON.stringify(r.body ?? {}) };
  };
  const deps = {
    loadConfig: () => loadHostConfig({ home, fetch: fleetFetch }),
    collect: async () => ({ envelopes: [{ entity_id: `e${++pass}`, payload: { status: "idle" } }], summary: { errors: 0 } }),
    fetcher: async (_url, options) => {
      keysUsed.push(options.headers["X-API-Key"]);
      return { ok: ingest(keysUsed.length).ok, status: ingest(keysUsed.length).status, body: { cancel: async () => {} } };
    },
    now: () => clock,
    sleep: async ms => { clock += ms; if (pass >= passes) process.emit("SIGTERM"); },
    refreshMs: 300000,
    inventoryEnvelope: async () => null,
    loadEnvelope: () => null,
  };
  return { home, deps, keysUsed, fleetCalls: () => fleetCalls };
}
const cfgBody = key => ({ revision: key, values: { ingest: { api_key: key } } });
const cache = home => join(home, ".cache", "herdr-engineering", "fleet-config.json");

test("loop: rotated ingest.api_key is used after the refresh interval, not before", async () => {
  const h = loopHarness({ passes: 5, fleet: n => ({ status: 200, body: cfgBody(n === 1 ? "k1" : "k2") }), ingest: () => ({ ok: true, status: 200 }) });
  // passes run at clock 0,100,200,300,400; the refresh is due when pass 4 ends (clock 300).
  const code = await main(["--loop", "100"], {}, h.deps);
  assert.equal(code, 0);
  assert.deepEqual(h.keysUsed, ["k1", "k1", "k1", "k1", "k2"]);
  assert.equal(h.fleetCalls(), 2);
});

test("loop: no refresh before the interval", async () => {
  const h = loopHarness({ passes: 2, fleet: () => ({ status: 200, body: cfgBody("k1") }), ingest: () => ({ ok: true, status: 200 }) });
  await main(["--loop", "100"], {}, h.deps);
  assert.equal(h.fleetCalls(), 1);
});

test("loop: an ingest 401 triggers an immediate refresh", async () => {
  const h = loopHarness({ passes: 2, fleet: n => ({ status: 200, body: cfgBody(n === 1 ? "old" : "new") }),
    ingest: n => (n === 1 ? { ok: false, status: 401 } : { ok: true, status: 200 }) });
  await main(["--loop", "100"], {}, h.deps);
  assert.deepEqual(h.keysUsed, ["old", "new"]);
  assert.equal(h.fleetCalls(), 2);
});

test("loop: a revoked token ends the loop with exit 2 and deletes the cache", async () => {
  const h = loopHarness({ passes: 9, fleet: n => (n === 1 ? { status: 200, body: cfgBody("k1") } : { status: 401 }),
    ingest: n => (n === 1 ? { ok: false, status: 403 } : { ok: true, status: 200 }) });
  const code = await main(["--loop", "100"], {}, h.deps);
  assert.equal(code, 2);
  assert.equal(existsSync(cache(h.home)), false);
  assert.equal(h.keysUsed.length, 1);
});

test("loop: a transient refresh failure falls back to the cache and keeps running", async () => {
  const h = loopHarness({ passes: 4, fleet: n => (n === 1 ? { status: 200, body: cfgBody("k1") } : { status: 503 }),
    ingest: () => ({ ok: true, status: 200 }) });
  assert.equal(await main(["--loop", "100"], {}, h.deps), 0);
  assert.deepEqual(h.keysUsed, ["k1", "k1", "k1", "k1"]);
});

test("loop: refresh failing with no cache keeps the live key and exit code 0", async () => {
  let h;
  h = loopHarness({ passes: 8, fleet: n => {
    if (n === 1) return { status: 200, body: cfgBody("k1") };
    rmSync(cache(h.home), { force: true });
    throw new TypeError("fetch failed");
  }, ingest: () => ({ ok: true, status: 200 }) });
  assert.equal(await main(["--loop", "100"], {}, h.deps), 0);
  assert.deepEqual(new Set(h.keysUsed), new Set(["k1"]));
  assert.equal(h.keysUsed.length, 8);
});

test("loop: a cache fallback never replaces a newer in-memory key", async () => {
  let h;
  h = loopHarness({ passes: 8, fleet: n => {
    if (n === 1) return { status: 200, body: cfgBody("k1") };
    if (n === 2) return { status: 200, body: cfgBody("k2") };
    writeFileSync(cache(h.home), JSON.stringify({ host_id: "h1", revision: "k1", values: { ingest: { api_key: "k1" } } }));
    throw new TypeError("fetch failed");
  }, ingest: () => ({ ok: true, status: 200 }) });
  assert.equal(await main(["--loop", "100"], {}, h.deps), 0);
  assert.deepEqual(h.keysUsed, ["k1", "k1", "k1", "k1", "k2", "k2", "k2", "k2"]);
  assert.equal(h.fleetCalls(), 3);
});
