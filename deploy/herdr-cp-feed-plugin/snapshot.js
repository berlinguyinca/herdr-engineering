#!/usr/bin/env node
// Read native named-server snapshots without changing any session lifecycle.
import { execFile } from "node:child_process";
import { realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { herdrBin } from "./enrich.js";
import { hostIdentity } from "./emit.js";
import { loadHostConfig, HostConfigUnavailable } from "./host-config.js";
import { gitMetadata } from "./git-metadata.js";
import { createInventoryReporter, createLoadReporter } from "./inventory.js";
import { createModelResolver } from "./session-model.js";

const TERMINAL_STATES = new Set([
  "failed", "error", "crashed", "finished", "complete", "completed", "closed",
  "stopped", "canceled", "cancelled", "succeeded", "done", "archived", "merged",
]);

function query(binary, args) {
  return new Promise(resolve => {
    execFile(binary, args, { timeout: 5000, maxBuffer: 512 * 1024 }, (error, stdout) => {
      try { resolve(error ? null : JSON.parse(stdout)); }
      catch { resolve(null); }
    });
  });
}

let defaultModelResolver = null;

export async function collectSnapshot(env, { queryNative = query, metadata = gitMetadata, now = Date.now, resolveModel } = {}) {
  resolveModel ??= (defaultModelResolver ??= createModelResolver({
    queryProcess: (server, paneId) => queryNative(herdrBin(env), ["--session", server, "pane", "process-info", "--pane", paneId]),
  }));
  const summary = { servers: 0, sessions: 0, linked: 0, skipped: 0, errors: 0, truncated: false };
  const envelopes = [];
  const deadline = now() + 20000;
  const binary = herdrBin(env);
  const registry = await queryNative(binary, ["session", "list", "--json"]);
  if (!Array.isArray(registry?.sessions)) return { envelopes, summary: { ...summary, errors: 1 } };
  const candidates = registry.sessions.filter(row => row?.running === true);
  if (candidates.length > 8) summary.truncated = true;
  const names = new Map();
  const sockets = new Map();
  for (const row of candidates) {
    names.set(row.name, (names.get(row.name) || 0) + 1);
    sockets.set(row.socket_path, (sockets.get(row.socket_path) || 0) + 1);
  }
  const host = hostIdentity(env);
  for (const server of candidates.slice(0, 8)) {
    if (now() >= deadline) { summary.truncated = true; break; }
    if (typeof server.name !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(server.name)
      || typeof server.socket_path !== "string" || !server.socket_path.startsWith("/")
      || names.get(server.name) !== 1 || sockets.get(server.socket_path) !== 1) {
      summary.errors++;
      continue;
    }
    const response = await queryNative(binary, ["--session", server.name, "api", "snapshot"]);
    const snapshot = response?.result?.snapshot;
    if (!Array.isArray(snapshot?.agents)) { summary.errors++; continue; }
    summary.servers++;
    const terminalCounts = new Map();
    const paneCounts = new Map();
    for (const pane of snapshot.agents) {
      terminalCounts.set(pane?.terminal_id, (terminalCounts.get(pane?.terminal_id) || 0) + 1);
      paneCounts.set(pane?.pane_id, (paneCounts.get(pane?.pane_id) || 0) + 1);
    }
    for (const pane of snapshot.agents) {
      if (envelopes.length >= 128 || now() >= deadline) { summary.truncated = true; break; }
      if (!pane || typeof pane.pane_id !== "string" || !pane.pane_id || pane.pane_id.length > 200
        || typeof pane.terminal_id !== "string" || !/^term_[A-Za-z0-9_-]{1,128}$/.test(pane.terminal_id)
        || terminalCounts.get(pane.terminal_id) !== 1 || paneCounts.get(pane.pane_id) !== 1
        || typeof pane.agent !== "string" || !pane.agent) { summary.skipped++; continue; }
      const cwd = pane.foreground_cwd || pane.cwd || null;
      const identity = await metadata(cwd);
      const payload = {
        host_id: host, pane_id: pane.pane_id, terminal_id: pane.terminal_id,
        herdr_session: server.name, agent_role: pane.agent,
        status: typeof pane.agent_status === "string" && pane.agent_status ? pane.agent_status : "unknown",
        cwd, ...identity,
      };
      const model = await resolveModel(server.name, pane).catch(() => null);
      if (model) payload.model = model;
      // Copy native evidence only; a session revision is never a Git revision.
      for (const key of ["revision", "state_change_seq", "interactive_ready", "launch_pending", "agent_session"]) {
        if (pane[key] !== undefined && pane[key] !== null) payload[key] = pane[key];
      }
      envelopes.push({ source: "herdr", entity_type: "session",
        entity_id: ["herdr", host, server.name, pane.pane_id].map(encodeURIComponent).join(":"),
        event_type: "session.snapshot", payload });
      if (identity.repository_id && identity.commit_sha) summary.linked++;
    }
  }
  summary.sessions = envelopes.length;
  return { envelopes, summary };
}

export function createSnapshotPublisher({ fetcher = fetch, now = Date.now, maxEntries = 256 } = {}) {
  const sent = new Map();
  return async function publish({ url, apiKey } = {}, envelopes) {
    let posted = 0, unchanged = 0;
    const headers = { "Content-Type": "application/json" };
    if (apiKey) headers["X-API-Key"] = apiKey;
    // Stop at the first failure rather than multiplying timeouts during outages.
    for (const envelope of envelopes) {
      const body = JSON.stringify(envelope);
      const previous = sent.get(envelope.entity_id);
      const terminal = TERMINAL_STATES.has(String(envelope.payload?.status || "").trim().toLowerCase());
      if (previous?.body === body && (terminal || now() - previous.at < 300000)) { unchanged++; continue; }
      try {
        const response = await fetcher(`${url}/api/v1/ingest`, {
          method: "POST", headers, body,
          redirect: "error", signal: AbortSignal.timeout(2000),
        });
        await response.body?.cancel();
        if (!response.ok) {
          const failure = { posted, unchanged, errors: 1 };
          if (response.status === 401 || response.status === 403) failure.authRejected = true;
          return failure;
        }
        sent.delete(envelope.entity_id);
        sent.set(envelope.entity_id, { body, at: now() });
        while (sent.size > Math.max(1, maxEntries)) sent.delete(sent.keys().next().value);
        posted++;
      } catch { return { posted, unchanged, errors: 1 }; }
    }
    // A missing/failed snapshot is not evidence that a previous session ended.
    return { posted, unchanged, errors: 0 };
  };
}

export async function publishSnapshot(target, envelopes, fetcher = fetch) {
  return createSnapshotPublisher({ fetcher })(target, envelopes);
}

export const CONFIG_REFRESH_MS = 300000;

// Returns the process exit code. Deps are injectable for tests.
export async function main(args = process.argv.slice(2), env = process.env, deps = {}) {
  const { loadConfig = loadHostConfig, collect = collectSnapshot, fetcher = fetch, now = Date.now,
    sleep = (ms, signal) => delay(ms, undefined, { signal }).catch(() => {}),
    refreshMs = CONFIG_REFRESH_MS, inventoryEnvelope = createInventoryReporter(),
    loadEnvelope = createLoadReporter() } = deps;
  const dryRun = args.includes("--dry-run");
  const loopIndex = args.indexOf("--loop");
  const interval = loopIndex < 0 ? null : Number(args[loopIndex + 1]);
  if (loopIndex >= 0 && (!Number.isFinite(interval) || interval < 30 || interval > 3600)) {
    throw new Error("--loop requires an interval between 30 and 3600 seconds");
  }
  const toTarget = cfg => ({ url: cfg?.url, apiKey: cfg?.apiKey });
  let target = toTarget(null);
  let loadedAt = now();
  // Only a fresh fetch replaces the live target. A cache fallback or a transient
  // failure keeps the config already in memory; only a revoked token (401) ends
  // the process. Returns false when the caller must exit 2.
  let loaded = false;
  const refresh = async ({ advanceOnStale = true } = {}) => {
    try {
      const cfg = await loadConfig();
      if (cfg.source === "fetch" || !loaded) { target = toTarget(cfg); loaded = true; loadedAt = now(); }
      else if (advanceOnStale) loadedAt = now();
      return true;
    } catch (error) {
      if (!(error instanceof HostConfigUnavailable)) throw error;
      if (error.revoked || !loaded) { process.stderr.write(`${error.message}\n`); return false; }
      if (advanceOnStale) loadedAt = now();
      return true;
    }
  };
  if (!dryRun && !(await refresh())) return 2;
  const controller = new AbortController();
  const publish = createSnapshotPublisher({ fetcher, now });
  const stop = () => controller.abort();
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  try {
    do {
      const result = await collect(env);
      // Fleet version inventory rides the same publisher; it is throttled by TTL.
      const inventory = await inventoryEnvelope(hostIdentity(env)).catch(() => null);
      if (inventory) result.envelopes.push(inventory);
      const load = loadEnvelope(hostIdentity(env));
      if (load) result.envelopes.push(load);
      const delivery = dryRun ? { posted: 0, unchanged: 0, errors: 0 } : await publish(target, result.envelopes);
      process.stdout.write(JSON.stringify({ ...result.summary, posted: delivery.posted,
        unchanged: delivery.unchanged,
        errors: result.summary.errors + delivery.errors, dry_run: dryRun }) + "\n");
      if (interval === null || controller.signal.aborted) break;
      if (!dryRun && (delivery.authRejected || now() - loadedAt >= refreshMs)) {
        // After an auth rejection, retry on the next pass if no fresh fetch came back.
        if (!(await refresh({ advanceOnStale: !delivery.authRejected }))) return 2;
      }
      await sleep(interval * 1000, controller.signal);
    } while (!controller.signal.aborted);
  } finally {
    process.removeListener("SIGINT", stop);
    process.removeListener("SIGTERM", stop);
  }
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  main().then(code => { process.exitCode = code; })
    .catch(() => { process.stderr.write("Native session snapshot collector failed.\n"); process.exitCode = 1; });
}
