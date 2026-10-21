import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { claudePlugins, piPackages, toolVersions, createInventoryReporter } from "./inventory.js";

function fakeHome() {
  const home = mkdtempSync(join(tmpdir(), "inv-"));
  mkdirSync(join(home, ".claude", "plugins"), { recursive: true });
  writeFileSync(join(home, ".claude", "plugins", "installed_plugins.json"), JSON.stringify({
    plugins: { "superpowers@claude-plugins-official": [{ version: "7.0.0" }] } }));
  mkdirSync(join(home, ".pi", "agent"), { recursive: true });
  writeFileSync(join(home, ".pi", "agent", "settings.json"), JSON.stringify({ packages: [
    "/h/.local/share/herdr-engineering/pi/releases/24c28ebc374476d3886f252e513387085054a3cb",
    "../../workspaces/pi-engineering-runtime"] }));
  return home;
}

test("claude plugin versions come from installed_plugins.json", () => {
  assert.deepEqual(claudePlugins(fakeHome()), { "claude:superpowers": "7.0.0" });
});

test("pi packages: pinned release shows short rev, checkouts show dev", () => {
  assert.deepEqual(piPackages(fakeHome()), {
    "pi:pi-engineering": "24c28ebc", "pi:pi-engineering-runtime": "dev" });
});

test("tool versions parse --version output and skip missing tools", async () => {
  const exec = (bin, _a, _o, cb) => bin === "claude" ? cb(null, "2.1.98 (Claude Code)\n")
    : bin === "pi" ? cb(null, "pi 1.3.0\n") : cb(new Error("ENOENT"));
  assert.deepEqual(await toolVersions({ exec }), { claude: "2.1.98", pi: "1.3.0" });
});

test("reporter throttles by TTL", async () => {
  let t = 0, calls = 0;
  const r = createInventoryReporter({ collect: async () => { calls++; return { tools: {}, plugins: {} }; },
    ttlMs: 1000, now: () => t });
  assert.equal((await r("h")).event_type, "host.inventory");
  t = 500; assert.equal(await r("h"), null);
  t = 1500; assert.ok(await r("h"));
  assert.equal(calls, 2);
});
