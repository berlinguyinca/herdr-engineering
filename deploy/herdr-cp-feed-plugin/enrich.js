#!/usr/bin/env node
// HerdR control-plane feed plugin — live session enrichment.
//
// The base event envelope carries only identity (host/mission/agent/status).
// This module queries the local herdr CLI + git to add operational detail so
// the control plane can show WHAT a session is doing, WHERE it runs, which
// branch it's on, its pid, and how to attach to it. Everything is best-effort:
// a failed herdr/git call yields a null detail field, never a crash.
//
//   herdr pane get <id>            -> agent, cwd, terminal_title (what it's doing)
//   herdr pane process-info --pane <id> -> foreground pid + cmdline
//   git -C <cwd> branch --show-current  -> branch (+ short commit)
//
// `mergeDetail` is pure and node-testable; the fetch helpers wrap subprocesses.

import os from "node:os";
import { homedir } from "node:os";
import { isAbsolute, join, normalize } from "node:path";
import { execFile } from "node:child_process";
import { statSync } from "node:fs";
import { gitMetadata } from "./git-metadata.js";

// Locate the herdr CLI. Prefer PATH; fall back to the per-user install dir that
// herdr commonly lives in (and that the non-interactive plugin env may miss).
export function herdrBin(env = {}) {
  if (env.HERDR_BIN) return env.HERDR_BIN;
  if (env.PATH) {
    for (const dir of env.PATH.split(":")) {
      if (dir) {
        const p = join(dir, "herdr");
        try {
          statSync(p).mode; // throws if not present
          return p;
        } catch {
          // try next
        }
      }
    }
  }
  return join(homedir(), ".local", "bin", "herdr");
}

function run(cmd, args, cwd) {
  return new Promise((resolve) => {
    execFile(cmd, args, { cwd, timeout: 5000, maxBuffer: 128 * 1024 },
      (err, stdout) => {
        if (err) return resolve(null);
        const out = String(stdout || "").trim();
        return resolve(out || null);
      });
  });
}

async function runJson(cmd, args) {
  const out = await run(cmd, args);
  if (!out) return null;
  try {
    const v = JSON.parse(out);
    return v && typeof v === "object" ? v : null;
  } catch {
    return null;
  }
}

function serverArgs(env) {
  const name = env.HERDR_SESSION_NAME || env.HERDR_SESSION;
  return name ? ["--session", name] : [];
}

// Native hooks can identify their server by socket instead of session name.
// Resolve only an exact, unique live registry entry; pane IDs and cwd are not
// unique across servers and must never establish a command route.
export async function resolveSessionEnv(env) {
  if (env.HERDR_SESSION_NAME || env.HERDR_SESSION) return env;
  const socket = env.HERDR_SOCKET_PATH;
  if (typeof socket !== "string" || socket.length > 4096 || !isAbsolute(socket) || /[\x00-\x1f]/.test(socket)) return env;
  const registry = await runJson(herdrBin(env), ["session", "list", "--json"]);
  if (!Array.isArray(registry?.sessions) || registry.sessions.length > 32) return env;
  const matches = registry.sessions.filter(row => row?.running === true
    && typeof row.name === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(row.name)
    && typeof row.socket_path === "string" && isAbsolute(row.socket_path)
    && normalize(row.socket_path) === normalize(socket));
  if (matches.length !== 1 || registry.sessions.filter(row => row?.running === true && row.name === matches[0].name).length !== 1) return env;
  return { ...env, HERDR_SESSION_NAME: matches[0].name };
}

// Pane info from `herdr pane get`. Returns { agent, cwd, summary } or null.
export async function paneDetail(env, paneId) {
  const bin = herdrBin(env);
  const v = await runJson(bin, [...serverArgs(env), "pane", "get", paneId]);
  const pane = v?.result?.pane || v?.pane;
  if (!pane) return null;
  const cwd = pane.cwd || pane.foreground_cwd || null;
  const summary = pane.terminal_title_stripped || pane.terminal_title || null;
  return { agent: pane.agent || null, cwd, summary,
    terminal_id: pane.terminal_id, agent_session: pane.agent_session,
    revision: pane.revision, interactive_ready: pane.interactive_ready,
    launch_pending: pane.launch_pending, state_change_seq: pane.state_change_seq };
}

// Foreground process info from `herdr pane process-info --pane <id>`.
// Returns { pid, cmdline } of the first foreground process, else null.
export async function processDetail(env, paneId) {
  const bin = herdrBin(env);
  const v = await runJson(bin, [...serverArgs(env), "pane", "process-info", "--pane", paneId]);
  const pi = v?.result?.process_info || v?.process_info;
  if (!pi) return null;
  const procs = Array.isArray(pi.foreground_processes)
    ? pi.foreground_processes : [];
  const p = procs[0];
  return {
    pid: String(p?.pid ?? pi.shell_pid ?? ""),
    cmdline: p?.cmdline || p?.argv?.join(" ") || null,
  };
}

// Current git branch + short commit for a directory. Returns "branch@commit".
export async function branchDetail(env, cwd) {
  if (!cwd) return null;
  const branch = await run("git", ["-C", cwd, "branch", "--show-current"], undefined);
  const commit = await run("git", ["-C", cwd, "rev-parse", "--short", "HEAD"], undefined);
  if (!branch && !commit) return null;
  return [branch || null, commit ? `@${commit}` : null].filter(Boolean).join("") || null;
}

// Command to attach to this session on the host that owns it.
//   local host -> "herdr workspace focus <ws>"
//   remote host -> "ssh <host> && herdr workspace focus <ws>"
export function attachCommand(env, host, ws) {
  const cmd = `herdr workspace focus ${ws}`;
  const local = (host || "") === (env?.HOSTNAME || os.hostname());
  return local ? cmd : `ssh ${host} && ${cmd}`;
}

// Pure: merge a fetched detail object into a session envelope's payload.
// Only non-null fields overwrite; identity fields are preserved.
export function mergeDetail(envelope, detail) {
  if (!envelope || envelope.entity_type !== "session" || !detail) return envelope;
  const payload = { ...(envelope.payload || {}) };
  for (const k of ["agent_role", "cwd", "branch", "pid", "cmdline", "summary", "attach", "terminal_id", "agent_session", "revision", "interactive_ready", "launch_pending", "state_change_seq", "repository_id", "commit_sha"]) {
    const v = detail[k];
    if (v !== undefined && v !== null && v !== "") {
      if (k === "agent_role" && payload.agent_role) continue; // prefer existing
      payload[k] = v;
    }
  }
  return { ...envelope, payload };
}

// Fetch live detail for a session envelope and merge it in. Best-effort.
export async function enrichSession(env, envelope) {
  if (!envelope || envelope.entity_type !== "session") return envelope;
  const paneId = envelope.payload?.pane_id || envelope.entity_id;
  const server = envelope.payload?.herdr_session;
  // New namespaced records with unknown server routing are telemetry-only.
  if (envelope.entity_id.startsWith("herdr:") && !server) return envelope;
  const routedEnv = server ? { ...env, HERDR_SESSION_NAME: server } : env;
  const ws = envelope.payload?.mission_id;
  const host = envelope.payload?.host_id;
  const [pane, proc] = await Promise.all([
    paneDetail(routedEnv, paneId).catch(() => null),
    processDetail(routedEnv, paneId).catch(() => null),
  ]);
  const cwd = pane?.cwd || proc?.cwd || null;
  const [branch, repository] = await Promise.all([
    branchDetail(env, cwd).catch(() => null),
    gitMetadata(cwd).catch(() => ({})),
  ]);
  const detail = {
    ...repository,
    agent_role: pane?.agent,
    terminal_id: pane?.terminal_id,
    agent_session: pane?.agent_session,
    revision: pane?.revision,
    interactive_ready: pane?.interactive_ready,
    launch_pending: pane?.launch_pending,
    state_change_seq: pane?.state_change_seq,
    cwd,
    summary: pane?.summary,
    pid: proc?.pid,
    cmdline: proc?.cmdline,
    branch,
    attach: attachCommand(env, host, ws),
  };
  return mergeDetail(envelope, detail);
}
