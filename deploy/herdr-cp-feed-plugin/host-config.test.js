// Tests for the central host-config client (parity with herdr_engineering/host_config.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadHostConfig, HostConfigUnavailable } from "./host-config.js";

const TOKEN = "placeholder-value-one";
const VALUE = "placeholder-value-two";

function makeHome(url = "http://cp.test") {
  const home = mkdtempSync(join(tmpdir(), "hostcfg-"));
  mkdirSync(join(home, ".config", "herdr-engineering"), { recursive: true });
  writeFileSync(join(home, ".config", "herdr-engineering", "host.json"),
    JSON.stringify({ control_plane_url: url, host_id: "h1", token: TOKEN }));
  return home;
}
const cachePath = home => join(home, ".cache", "herdr-engineering", "fleet-config.json");
const ok = body => ({ status: 200, text: async () => JSON.stringify(body) });
const reply = status => ({ status, text: async () => "" });
const GOOD = { revision: "r1", values: { ingest: { api_key: VALUE }, ci: { url: "http://ci" } } };

function recorder(handler) {
  const calls = [];
  const fetch = async (url, init) => { calls.push({ url, init }); return handler(url, init, calls); };
  return { fetch, calls };
}

test("fetches, exposes values and caches with mode 0600", async () => {
  const home = makeHome();
  const { fetch, calls } = recorder(() => ok(GOOD));
  const cfg = await loadHostConfig({ home, fetch });
  assert.equal(cfg.url, "http://cp.test");
  assert.equal(cfg.hostId, "h1");
  assert.equal(cfg.revision, "r1");
  assert.equal(cfg.apiKey, VALUE);
  assert.equal(cfg.get("ci.url"), "http://ci");
  assert.equal(cfg.get("ci.missing", "fb"), "fb");
  assert.equal(calls[0].url, "http://cp.test/api/v1/fleet/config");
  assert.equal(calls[0].init.headers["X-Host-Token"], TOKEN);
  assert.equal(calls[0].init.redirect, "manual");
  assert.equal(statSync(cachePath(home)).mode & 0o777, 0o600);
  assert.equal(statSync(join(home, ".cache", "herdr-engineering")).mode & 0o777, 0o700);
});

test("posts applied revision best-effort", async () => {
  const home = makeHome();
  const { fetch, calls } = recorder(url => {
    if (url.endsWith("/applied")) throw new Error("boom");
    return ok(GOOD);
  });
  const cfg = await loadHostConfig({ home, fetch });
  assert.equal(cfg.revision, "r1");
  const applied = calls.find(c => c.url.endsWith("/api/v1/fleet/config/applied"));
  assert.equal(applied.init.method, "POST");
  assert.deepEqual(JSON.parse(applied.init.body), { host_id: "h1", revision: "r1" });
});

test("uses cache when fetch rejects", async () => {
  const home = makeHome();
  await loadHostConfig({ home, fetch: recorder(() => ok(GOOD)).fetch });
  const cfg = await loadHostConfig({ home, fetch: async () => { throw new TypeError("fetch failed"); } });
  assert.equal(cfg.apiKey, VALUE);
  assert.equal(cfg.revision, "r1");
});

test("5xx uses cache", async () => {
  const home = makeHome();
  await loadHostConfig({ home, fetch: recorder(() => ok(GOOD)).fetch });
  const cfg = await loadHostConfig({ home, fetch: async () => reply(503) });
  assert.equal(cfg.apiKey, VALUE);
});

test("malformed reply uses cache", async () => {
  const home = makeHome();
  await loadHostConfig({ home, fetch: recorder(() => ok(GOOD)).fetch });
  const cfg = await loadHostConfig({ home, fetch: async () => ({ status: 200, text: async () => "not json" }) });
  assert.equal(cfg.revision, "r1");
});

test("401 clears the cache and throws", async () => {
  const home = makeHome();
  await loadHostConfig({ home, fetch: recorder(() => ok(GOOD)).fetch });
  assert.ok(existsSync(cachePath(home)));
  await assert.rejects(loadHostConfig({ home, fetch: async () => reply(401) }), HostConfigUnavailable);
  assert.equal(existsSync(cachePath(home)), false);
});

test("no cache and unreachable throws host config unavailable", async () => {
  const home = makeHome();
  await assert.rejects(
    loadHostConfig({ home, fetch: async () => { throw new TypeError("fetch failed"); } }),
    err => err instanceof HostConfigUnavailable && /^host config unavailable: /.test(err.message));
});

test("missing host.json throws", async () => {
  const home = mkdtempSync(join(tmpdir(), "hostcfg-"));
  await assert.rejects(loadHostConfig({ home, fetch: async () => ok(GOOD) }), /host config unavailable/);
});

test("errors never contain the token or values", async () => {
  const home = makeHome();
  await loadHostConfig({ home, fetch: recorder(() => ok(GOOD)).fetch });
  const failures = [
    async () => reply(401),
    async () => reply(403),
    async () => { throw new Error(`leaky ${TOKEN} ${VALUE}`); },
  ];
  for (const f of failures) {
    const h = makeHome();
    try { await loadHostConfig({ home: h, fetch: f }); assert.fail("should throw"); }
    catch (err) {
      assert.ok(err instanceof HostConfigUnavailable);
      assert.ok(!err.message.includes(TOKEN) && !err.message.includes(VALUE), err.message);
    }
  }
});

test("3xx is not followed and keeps the cache (real server)", async () => {
  let targetHits = 0;
  let sawToken = false;
  const target = http.createServer((req, res) => { targetHits++; sawToken ||= !!req.headers["x-host-token"]; res.end("{}"); });
  await new Promise(r => target.listen(0, "127.0.0.1", r));
  const tport = target.address().port;
  const cp = http.createServer((req, res) => {
    res.writeHead(302, { Location: `http://127.0.0.1:${tport}/stolen` }); res.end();
  });
  await new Promise(r => cp.listen(0, "127.0.0.1", r));
  try {
    const home = makeHome(`http://127.0.0.1:${cp.address().port}`);
    await loadHostConfig({ home, fetch: recorder(() => ok(GOOD)).fetch });
    const cfg = await loadHostConfig({ home });   // real global fetch
    assert.equal(cfg.apiKey, VALUE);
    assert.ok(existsSync(cachePath(home)));
    assert.equal(targetHits, 0);
    assert.equal(sawToken, false);
    const home2 = makeHome(`http://127.0.0.1:${cp.address().port}`);
    await assert.rejects(loadHostConfig({ home: home2 }), HostConfigUnavailable);
    assert.equal(targetHits, 0);
  } finally {
    cp.close(); target.close();
  }
});

test("get(): missing section, null value and non-object node fall back", async () => {
  const home = makeHome();
  const body = { revision: "r1", values: { ingest: { api_key: VALUE, nothing: null }, scalar: "text" } };
  const cfg = await loadHostConfig({ home, fetch: async () => ok(body) });
  assert.equal(cfg.get("absent.key", "fb"), "fb");
  assert.equal(cfg.get("ingest.nothing", "fb"), "fb");
  assert.equal(cfg.get("scalar.key", "fb"), "fb");
  assert.equal(cfg.get("nodot", "fb"), "fb");
  assert.equal(cfg.get("ingest.api_key", "fb"), VALUE);
});

test("array values are malformed and fall back to the cache", async () => {
  const home = makeHome();
  await loadHostConfig({ home, fetch: recorder(() => ok(GOOD)).fetch });
  const cfg = await loadHostConfig({ home, fetch: async () => ok({ revision: "r2", values: [1, 2] }) });
  assert.equal(cfg.revision, "r1");
  const empty = makeHome();
  await assert.rejects(loadHostConfig({ home: empty, fetch: async () => ok({ revision: "r2", values: [] }) }), HostConfigUnavailable);
});

test("array values in the cache are ignored; only 401 sets revoked", async () => {
  const home = makeHome();
  mkdirSync(join(home, ".cache", "herdr-engineering"), { recursive: true });
  writeFileSync(cachePath(home), JSON.stringify({ host_id: "h1", revision: "r1", values: [1] }));
  const down = async () => { throw new TypeError("fetch failed"); };
  await assert.rejects(loadHostConfig({ home, fetch: down }), err => err instanceof HostConfigUnavailable && err.revoked === false);
  await assert.rejects(loadHostConfig({ home, fetch: async () => reply(401) }), err => err.revoked === true);
});

test("source is fetch on a live reply and cache on fallback", async () => {
  const home = makeHome();
  assert.equal((await loadHostConfig({ home, fetch: async () => ok(GOOD) })).source, "fetch");
  assert.equal((await loadHostConfig({ home, fetch: async () => reply(503) })).source, "cache");
});
