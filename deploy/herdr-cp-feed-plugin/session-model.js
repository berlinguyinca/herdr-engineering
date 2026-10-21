// Which model is a native agent session using? Read it from the agent's own
// session log rather than guessing: pi records every model_change, Claude Code
// stamps each assistant message. Best-effort; null means "not determinable".
import { closeSync, fstatSync, openSync, readSync, readdirSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve, sep } from "node:path";

const TAIL_BYTES = 256 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function tail(path) {
  let fd;
  try {
    fd = openSync(path, "r");
    const { size } = fstatSync(fd);
    const length = Math.min(size, TAIL_BYTES);
    const buffer = Buffer.alloc(length);
    readSync(fd, buffer, 0, length, size - length);
    return buffer.toString("utf8");
  } catch { return null; } finally { if (fd !== undefined) try { closeSync(fd); } catch { /* ignore */ } }
}

export function piModel(text) {
  let found = null;
  for (const line of String(text).split("\n")) {
    if (!line.includes('"model_change"')) continue;
    try {
      const e = JSON.parse(line);
      if (e.type === "model_change" && typeof e.modelId === "string" && e.modelId) {
        found = typeof e.provider === "string" && e.provider ? `${e.provider}/${e.modelId}` : e.modelId;
      }
    } catch { /* a cut first line or partial write */ }
  }
  return found;
}

export function claudeModel(text) {
  let found = null;
  for (const line of String(text).split("\n")) {
    if (!line.includes('"model"')) continue;
    try {
      const m = JSON.parse(line)?.message?.model;
      if (typeof m === "string" && m && m !== "<synthetic>") found = m;
    } catch { /* partial line */ }
  }
  return found;
}

export function sessionModel(agent, evidence, { home = homedir(), read = tail } = {}) {
  const value = evidence && typeof evidence === "object" ? evidence.value : null;
  if (typeof value !== "string" || !value) return null;
  if (agent === "pi" && evidence.kind === "path") {
    const root = resolve(home, ".pi", "agent", "sessions") + sep;
    const path = resolve(value);
    if (!path.startsWith(root) || !path.endsWith(".jsonl")) return null;
    const text = read(path);
    return text ? piModel(text) : null;
  }
  if (agent === "claude" && evidence.kind === "id" && UUID.test(value)) {
    const projects = join(home, ".claude", "projects");
    let dirs = [];
    try { dirs = readdirSync(projects); } catch { return null; }
    for (const dir of dirs) {
      const path = join(projects, dir, `${value}.jsonl`);
      if (existsSync(path)) { const text = read(path); return text ? claudeModel(text) : null; }
    }
  }
  return null;
}

// ---- pi: pane -> foreground pid -> control descriptor -> session log -------
const SESSION_ID = /^[0-9A-Za-z][0-9A-Za-z-]{7,63}$/;

export function controlDir(env = process.env, uid = process.getuid?.()) {
  if (env.PI_ENGINEERING_CONTROL_DIR) return resolve(env.PI_ENGINEERING_CONTROL_DIR);
  return env.XDG_RUNTIME_DIR ? join(env.XDG_RUNTIME_DIR, "pi-engineering")
    : join(env.TMPDIR || "/tmp", `pi-engineering-${uid}`);
}

function descriptorsByPid(dir, { list = readdirSync, read = tail } = {}) {
  const byPid = new Map();
  let names = [];
  try { names = list(dir); } catch { return byPid; }
  for (const name of names.filter(n => n.endsWith(".json")).slice(0, 1024)) {
    try {
      const d = JSON.parse(read(join(dir, name)) ?? "");
      if (Number.isInteger(d.pid) && typeof d.sessionId === "string" && SESSION_ID.test(d.sessionId)) byPid.set(d.pid, d.sessionId);
    } catch { /* unreadable or foreign file */ }
  }
  return byPid;
}

function sessionFile(sessionId, root, { list = readdirSync } = {}) {
  let dirs = [];
  try { dirs = list(root); } catch { return null; }
  for (const dir of dirs) {
    try {
      const hit = list(join(root, dir)).find(n => n.endsWith(`_${sessionId}.jsonl`));
      if (hit) return join(root, dir, hit);
    } catch { /* not a directory */ }
  }
  return null;
}

// Resolves a pane's model, caching per terminal so the 30s loop does not re-run
// `herdr pane process-info` for every pane every time.
export function createModelResolver({ queryProcess, env = process.env, home = homedir(), ttlMs = 300_000,
  now = Date.now, read = tail, list = readdirSync } = {}) {
  const cache = new Map();
  return async function resolveModel(server, pane) {
    const key = `${server}:${pane.terminal_id}`;
    const hit = cache.get(key);
    if (hit && now() - hit.at < ttlMs) return hit.model;
    let model = null;
    if (pane.agent === "pi") {
      const info = await queryProcess(server, pane.pane_id).catch(() => null);
      const pids = (info?.result?.process_info?.foreground_processes ?? []).map(p => p?.pid).filter(Number.isInteger);
      const sessions = descriptorsByPid(controlDir(env), { list, read });
      const sessionId = pids.map(pid => sessions.get(pid)).find(Boolean);
      const file = sessionId && sessionFile(sessionId, join(home, ".pi", "agent", "sessions"), { list });
      const text = file && read(file);
      model = text ? piModel(text) : null;
    } else if (pane.agent === "claude") {
      // Claude Code records pid -> sessionId in ~/.claude/sessions/<pid>.json.
      let evidence = pane.agent_session;
      if (!evidence) {
        const info = await queryProcess(server, pane.pane_id).catch(() => null);
        const pids = (info?.result?.process_info?.foreground_processes ?? []).map(p => p?.pid).filter(Number.isInteger);
        for (const pid of pids) {
          try {
            const sessionId = JSON.parse(read(join(home, ".claude", "sessions", `${pid}.json`)) ?? "").sessionId;
            if (typeof sessionId === "string") { evidence = { kind: "id", value: sessionId }; break; }
          } catch { /* not a claude process */ }
        }
      }
      model = sessionModel("claude", evidence, { home, read });
    } else {
      model = sessionModel(pane.agent, pane.agent_session, { home, read });
    }
    cache.set(key, { at: now(), model });
    while (cache.size > 512) cache.delete(cache.keys().next().value);
    return model;
  };
}
