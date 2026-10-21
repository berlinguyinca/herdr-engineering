// Per-host tool and plugin version inventory, reported to the control plane so
// the dashboard can show fleet drift. Everything is best-effort: a missing tool
// is simply absent from the report, never an error.
import { execFile } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { homedir, loadavg, cpus, totalmem, freemem } from "node:os";
import { statfsSync } from "node:fs";
import { basename, join } from "node:path";

const VERSION = /\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?/;

// Service managers hand us a minimal PATH, but agent CLIs often live in user
// directories (npm global bins under nvm, ~/.local/bin, Homebrew). Search those
// too, newest nvm node first so a CLI's `#!/usr/bin/env node` finds a modern one.
export function probePath(env = process.env, home = homedir(), list = readdirSync) {
  const nvm = join(home, ".nvm", "versions", "node");
  let versions = [];
  try { versions = list(nvm); } catch { /* no nvm */ }
  const newest = versions.filter(v => /^v\d+\.\d+\.\d+$/.test(v))
    .sort((a, b) => b.localeCompare(a, undefined, { numeric: true })).map(v => join(nvm, v, "bin"));
  const dirs = [join(home, ".local", "bin"), ...newest, "/opt/homebrew/bin", "/usr/local/bin"];
  return [...new Set([...dirs, ...String(env.PATH ?? "").split(":").filter(Boolean)])].join(":");
}

function run(binary, args, exec = execFile, env) {
  return new Promise(resolve => {
    exec(binary, args, { timeout: 5000, maxBuffer: 64 * 1024, ...(env ? { env } : {}) }, (error, stdout) => {
      resolve(error ? null : String(stdout));
    });
  });
}

export async function toolVersions({ exec = execFile } = {}) {
  const probes = {
    claude: ["claude", ["--version"]],
    codex: ["codex", ["--version"]],
    pi: ["pi", ["--version"]],
    herdr: ["herdr", ["--version"]],
    "herdr-eng": ["herdr-eng", ["--version"]],
    node: ["node", ["--version"]],
  };
  const out = {};
  const env = { ...process.env, PATH: probePath() };
  await Promise.all(Object.entries(probes).map(async ([name, [bin, args]]) => {
    // node is reported as the interpreter this service itself runs under.
    const text = await run(bin, args, exec, name === "node" ? undefined : env);
    const match = text && text.match(VERSION);
    if (match) out[name] = match[0];
  }));
  return out;
}

function readJson(path) {
  try { return JSON.parse(readFileSync(path, "utf8")); } catch { return null; }
}

// Claude Code plugins: installed_plugins.json maps "name@marketplace" to installs.
export function claudePlugins(home = homedir()) {
  const data = readJson(join(home, ".claude", "plugins", "installed_plugins.json"));
  const out = {};
  for (const [id, installs] of Object.entries(data?.plugins ?? {})) {
    const first = Array.isArray(installs) ? installs[0] : null;
    if (first?.version) out[`claude:${id.split("@")[0]}`] = String(first.version);
  }
  return out;
}

// Pi packages: pinned releases live in a directory named for the commit; keep the
// short revision. Dev checkouts report as "dev" so they stand out as drift.
export function piPackages(home = homedir()) {
  const settings = readJson(join(home, ".pi", "agent", "settings.json"));
  const out = {};
  for (const entry of settings?.packages ?? []) {
    if (typeof entry !== "string") continue;
    const name = basename(entry.replace(/\/releases\/[0-9a-f]{40}$/, ""));
    const release = entry.match(/\/releases\/([0-9a-f]{40})$/);
    const key = name === "pi" || /herdr-engineering\/pi/.test(entry)
      ? "pi-engineering" : name;
    out[`pi:${key}`] = release ? release[1].slice(0, 8) : "dev";
  }
  return out;
}

export async function collectInventory(opts = {}) {
  const home = opts.home ?? homedir();
  return {
    tools: await toolVersions(opts),
    plugins: { ...claudePlugins(home), ...piPackages(home) },
  };
}

// Collector with a TTL so the 30s snapshot loop only shells out every few minutes.
export function createInventoryReporter({ collect = collectInventory, ttlMs = 600_000,
  now = Date.now } = {}) {
  let last = null;
  return async function maybeInventoryEnvelope(host) {
    if (last !== null && now() - last < ttlMs) return null;
    const inventory = await collect();
    last = now();
    return { entity_type: "host", entity_id: host, event_type: "host.inventory",
             payload: { host_name: host, inventory } };
  };
}

// Coarse host load for the Hosts page: percentages, rounded, no process detail.
export function hostLoad({ load = loadavg, ncpu = () => cpus().length, total = totalmem,
  free = freemem, statfs = statfsSync, path = homedir() } = {}) {
  const out = {};
  const n = Math.max(1, ncpu());
  out.cpu = Math.min(100, Math.round((load()[0] / n) * 100));
  const t = total();
  if (t > 0) out.mem = Math.round(((t - free()) / t) * 100);
  try {
    const fs = statfs(path);
    if (fs.blocks > 0) out.disk = Math.round((1 - fs.bavail / fs.blocks) * 100);
  } catch { /* unsupported filesystem */ }
  return out;
}

export function createLoadReporter({ load = hostLoad, ttlMs = 120_000, now = Date.now } = {}) {
  let last = null;
  return function maybeLoadEnvelope(host) {
    if (last !== null && now() - last < ttlMs) return null;
    last = now();
    return { entity_type: "host", entity_id: host, event_type: "host.heartbeat",
             payload: { host_name: host, ...load() } };
  };
}
