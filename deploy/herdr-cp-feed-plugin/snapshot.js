#!/usr/bin/env node
// Read native named-server snapshots without changing any session lifecycle.
import { execFile } from "node:child_process";
import { pathToFileURL } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { herdrBin } from "./enrich.js";
import { hostIdentity, resolveUrl } from "./emit.js";
import { gitMetadata } from "./git-metadata.js";

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

export async function collectSnapshot(env, { queryNative = query, metadata = gitMetadata, now = Date.now } = {}) {
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
  return async function publish(env, envelopes) {
    let posted = 0, unchanged = 0;
    const headers = { "Content-Type": "application/json" };
    if (env.HERDR_CP_API_KEY) headers["X-API-Key"] = env.HERDR_CP_API_KEY;
    // Stop at the first failure rather than multiplying timeouts during outages.
    for (const envelope of envelopes) {
      const body = JSON.stringify(envelope);
      const previous = sent.get(envelope.entity_id);
      const terminal = TERMINAL_STATES.has(String(envelope.payload?.status || "").trim().toLowerCase());
      if (previous?.body === body && (terminal || now() - previous.at < 300000)) { unchanged++; continue; }
      try {
        const response = await fetcher(`${resolveUrl(env)}/api/v1/ingest`, {
          method: "POST", headers, body,
          redirect: "error", signal: AbortSignal.timeout(2000),
        });
        await response.body?.cancel();
        if (!response.ok) return { posted, unchanged, errors: 1 };
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

export async function publishSnapshot(env, envelopes, fetcher = fetch) {
  return createSnapshotPublisher({ fetcher })(env, envelopes);
}

export async function main(args = process.argv.slice(2), env = process.env) {
  const dryRun = args.includes("--dry-run");
  const loopIndex = args.indexOf("--loop");
  const interval = loopIndex < 0 ? null : Number(args[loopIndex + 1]);
  if (loopIndex >= 0 && (!Number.isFinite(interval) || interval < 30 || interval > 3600)) {
    throw new Error("--loop requires an interval between 30 and 3600 seconds");
  }
  const controller = new AbortController();
  const publish = createSnapshotPublisher();
  const stop = () => controller.abort();
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  try {
    do {
      const result = await collectSnapshot(env);
      const delivery = dryRun ? { posted: 0, unchanged: 0, errors: 0 } : await publish(env, result.envelopes);
      process.stdout.write(JSON.stringify({ ...result.summary, posted: delivery.posted,
        unchanged: delivery.unchanged,
        errors: result.summary.errors + delivery.errors, dry_run: dryRun }) + "\n");
      if (interval === null || controller.signal.aborted) break;
      await delay(interval * 1000, undefined, { signal: controller.signal }).catch(() => {});
    } while (!controller.signal.aborted);
  } finally {
    process.removeListener("SIGINT", stop);
    process.removeListener("SIGTERM", stop);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { process.stderr.write("Native session snapshot collector failed.\n"); process.exitCode = 1; });
}
