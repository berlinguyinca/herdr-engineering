// Per-host tool and plugin version inventory, reported to the control plane so
// the dashboard can show fleet drift. Everything is best-effort: a missing tool
// is simply absent from the report, never an error.
import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, join } from "node:path";

const VERSION = /\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?/;

function run(binary, args, exec = execFile) {
  return new Promise(resolve => {
    exec(binary, args, { timeout: 5000, maxBuffer: 64 * 1024 }, (error, stdout) => {
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
  await Promise.all(Object.entries(probes).map(async ([name, [bin, args]]) => {
    const text = await run(bin, args, exec);
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
