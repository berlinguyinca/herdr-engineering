// Central fleet configuration client for the plugin (parity with
// herdr_engineering/host_config.py). Node built-ins only.
//
// Reads ~/.config/herdr-engineering/host.json {control_plane_url, host_id, token}
// (written by `herdr-eng fleet join`), fetches GET {url}/api/v1/fleet/config and
// caches it in ~/.cache/herdr-engineering/fleet-config.json (0600, dir 0700).
// A revoked token (401) deletes the cache. Redirects are never followed and the
// token and values never appear in an error message.
import { homedir } from "node:os";
import { join, dirname } from "node:path";
import { readFileSync, writeFileSync, mkdirSync, renameSync, unlinkSync, openSync, closeSync, fsyncSync } from "node:fs";
import { randomBytes } from "node:crypto";

const HOST_JSON_REL = [".config", "herdr-engineering", "host.json"];
const CACHE_REL = [".cache", "herdr-engineering", "fleet-config.json"];
const FLEET_PATH = "/api/v1/fleet/config";
const APPLIED_PATH = "/api/v1/fleet/config/applied";
const JOIN_HINT = "run `herdr-eng fleet join` for this host on the control-plane host";

export class HostConfigUnavailable extends Error {
  constructor(reason) {
    super(`host config unavailable: ${reason}`);
    this.name = "HostConfigUnavailable";
  }
}

export function readHostJson(home = homedir()) {
  const path = join(home, ...HOST_JSON_REL);
  let data;
  try { data = JSON.parse(readFileSync(path, "utf8")); }
  catch { throw new HostConfigUnavailable(`host.json missing or unreadable at ${path}; ${JOIN_HINT}`); }
  const { control_plane_url: url, host_id: hostId, token } = data ?? {};
  if (![url, hostId, token].every(x => typeof x === "string" && x)) {
    throw new HostConfigUnavailable(`host.json at ${path} is incomplete; ${JOIN_HINT}`);
  }
  return { url: url.replace(/\/+$/, ""), hostId, token };
}

function writeCache(path, payload) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const tmp = join(dirname(path), `.fleet-config.${randomBytes(6).toString("hex")}.tmp`);
  try {
    writeFileSync(tmp, JSON.stringify(payload), { mode: 0o600, flag: "wx" });
    const fd = openSync(tmp, "r");
    try { fsyncSync(fd); } finally { closeSync(fd); }
    renameSync(tmp, path);
  } catch (err) {
    try { unlinkSync(tmp); } catch { /* already gone */ }
    throw err;
  }
}

function readCache(path, hostId) {
  try {
    const data = JSON.parse(readFileSync(path, "utf8"));
    if (data.host_id !== hostId) return null;
    if (typeof data.revision !== "string" || !data.values || typeof data.values !== "object") return null;
    return { revision: data.revision, values: data.values };
  } catch { return null; }
}

function build(url, hostId, revision, values) {
  return {
    url, hostId, revision, values,
    apiKey: values?.ingest?.api_key ?? null,
    get(dottedKey, fallback) {
      const i = dottedKey.indexOf(".");
      const node = values?.[dottedKey.slice(0, i)];
      const key = dottedKey.slice(i + 1);
      if (i < 0 || !node || typeof node !== "object" || !(key in node) || node[key] == null) return fallback;
      return node[key];
    },
  };
}

export async function loadHostConfig({ home = homedir(), fetch = globalThis.fetch, timeoutMs = 3000 } = {}) {
  const { url, hostId, token } = readHostJson(home);
  const cachePath = join(home, ...CACHE_REL);
  const request = (path, init = {}) => fetch(url + path, {
    ...init,
    headers: { "X-Host-Token": token, Accept: "application/json", ...(init.headers ?? {}) },
    redirect: "manual",
    signal: AbortSignal.timeout(timeoutMs),
  });

  let status = null, payload = null;
  try {
    const res = await request(FLEET_PATH);
    status = res.status;
    if (status === 200) {
      try { payload = JSON.parse(await res.text()); } catch { payload = null; }
    }
  } catch { status = null; }   // network failure or timeout: fall back to the cache

  if (status === 401) {
    try { unlinkSync(cachePath); } catch { /* none */ }
    throw new HostConfigUnavailable(
      `the control plane rejected this host's token (revoked or unknown); cached configuration was discarded; ${JOIN_HINT}`);
  }
  if (status === 200 && payload && typeof payload.revision === "string"
      && payload.values && typeof payload.values === "object" && !Array.isArray(payload.values)) {
    const { revision, values } = payload;
    try { writeCache(cachePath, { host_id: hostId, revision, values }); } catch { /* cache is optional */ }
    try {
      await request(APPLIED_PATH, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host_id: hostId, revision }),
      });
    } catch { /* best effort */ }
    return build(url, hostId, revision, values);
  }
  if (status !== null && status >= 400 && status < 500) {
    throw new HostConfigUnavailable(`the control plane refused the configuration request (HTTP ${status})`);
  }
  const cached = readCache(cachePath, hostId);   // unreachable, 3xx, 5xx or malformed
  if (!cached) throw new HostConfigUnavailable(`control plane at ${url} unreachable and no cached configuration exists`);
  return build(url, hostId, cached.revision, cached.values);
}
