// Real-behaviour test for the Pi extension that makes handoffs automatic.
//
// Nothing here stubs our own code. The extension is imported verbatim from
// herdr_engineering/extension_data/herdr-handoff-autopilot.ts and every verdict from
// real `herdr-eng` CLI in this checkout (HERDR_ENG_ARGS below is the JSON argv install.sh gives
// pi: the venv interpreter running `herdr_engineering.cli`, so a wheel install and this
// checkout behave alike). Policy,
// the per-session ledger and the prompt text are exercised through that CLI;
// tests/test_handoff_autopilot.py covers them directly.
//
// The only stand-in is the Pi host: pi.on / pi.sendUserMessage cannot be driven headlessly,
// so a recording host implements the documented API surface (see
// @earendil-works/pi-coding-agent dist/extensions/types.d.ts) and we assert exactly what the
// extension hands it. CI runs this file with `node --test packages/*.test.js`.
//
// The extension must do no work on import - setup happens only when the host calls the
// default export - so tests can await a subscription without racing background work.

import { spawn } from "node:child_process";
import { mkdtemp, mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const repo = dirname(here);
const extension = join(
  repo,
  "herdr_engineering",
  "extension_data",
  "herdr-handoff-autopilot.ts",
);
const python = process.env.HERDR_TEST_PYTHON ?? "python3";
const skip = process.features.typescript
  ? false
  : "node cannot import a .ts extension without native TypeScript";

const settle = () => new Promise((resolve) => setTimeout(resolve, 4000));

// A fresh HOME/XDG per case, so the CLI's config and trigger ledger are real files.
async function sandbox({ percent = 50, enabled = true } = {}) {
  const root = await mkdtemp(join(tmpdir(), "herdr-autopilot-"));
  const home = join(root, "home");
  const state = join(root, "state");
  const work = join(root, "work");
  await mkdir(join(home, ".config", "herdr-engineering"), { recursive: true });
  await mkdir(state, { recursive: true });
  await mkdir(work, { recursive: true });
  await writeFile(
    join(home, ".config", "herdr-engineering", "config.yaml"),
    `handoff_autopilot:\n  enabled: ${enabled ? "true" : "false"}\n  context_percent: ${percent}\n`,
  );
  const ledgerPath = join(state, "herdr-engineering", "handoff-autopilot.json");
  return {
    home,
    state,
    work,
    ledger: async () => JSON.parse(await readFile(ledgerPath, "utf8")),
    hasLedger: async () => {
      try {
        await stat(ledgerPath);
        return true;
      } catch {
        return false;
      }
    },
  };
}

// The recorded Pi host: implements only the documented surface the extension may use.
function recordingHost({ usage, sessionId = "s-node" }) {
  const handlers = new Map();
  const injected = [];
  const notifications = [];
  const sessionManager = {
    getSessionId: () => sessionId,
    getBranch: () => [],
    getType: () => undefined,
    getEntry: () => undefined,
    getTree: () => [],
  };
  const ctx = {
    model: { contextWindow: 200000 },
    sessionId,
    sessionManager,
    getContextUsage: () => usage,
    getSelector: () => ({ get: () => undefined }),
    ui: { notify: (message, level) => notifications.push([message, level]) },
    getState: () => ({
      thinkingLevel: "medium",
      isStreaming: false,
      tools: [],
      messages: [],
    }),
  };
  const api = {
    on: (event, callback) => handlers.set(event, callback),
    sendUserMessage: (message, options) =>
      injected.push([message, options ?? {}]),
    exec: (path, args, options) =>
      new Promise((resolve, reject) => {
        const child = spawn(path, args, { cwd: options.cwd, env: options.env });
        let stdout = "";
        let stderr = "";
        child.stdout.on("data", (chunk) => {
          stdout += chunk;
        });
        child.stderr.on("data", (chunk) => {
          stderr += chunk;
        });
        child.on("error", reject);
        child.on("close", (code) =>
          resolve({ code: code ?? 1, stdout, stderr }),
        );
      }),
  };
  return { api, ctx, handlers, injected, notifications };
}

async function runTurn(box, { usage, sessionId = "s-node" }, turns = 1) {
  const host = recordingHost({ usage, sessionId });
  const mod = await import(`${extension}?case=${Math.random()}`);
  mod.default(host.api);
  const turnEnd = host.handlers.get("turn_end");
  assert.equal(
    typeof turnEnd,
    "function",
    "the extension must subscribe to turn_end",
  );
  // The extension resolves the CLI and its state through the real process environment.
  process.env.HOME = box.home;
  process.env.USERPROFILE = box.home;
  process.env.XDG_STATE_HOME = box.state;
  process.env.XDG_CONFIG_HOME = join(box.home, ".config");
  process.env.HERDR_ENG_ARGS = JSON.stringify([
    python,
    "-m",
    "herdr_engineering.cli",
  ]);
  delete process.env.HERDR_HANDOFF_AUTOPILOT;
  for (let turn = 0; turn < turns; turn += 1) {
    await turnEnd({ toolResults: [], textContent: "", messages: [] }, host.ctx);
    await settle();
  }
  return host;
}

test(
  "setup registers the turn_end subscription and nothing else",
  { skip },
  async () => {
    const host = recordingHost({
      usage: { tokensUsed: 0, contextWindow: 0, percent: null },
    });
    const mod = await import(`${extension}?case=setup-${Math.random()}`);
    mod.default(host.api);
    assert.deepEqual([...host.handlers.keys()], ["turn_end"]);
  },
);

test(
  "a full context injects the handoff instruction once and records it",
  { skip },
  async () => {
    const box = await sandbox();
    const host = await runTurn(
      box,
      { usage: { tokensUsed: 170000, contextWindow: 200000, percent: 85 } },
      2,
    );

    assert.equal(
      host.injected.length,
      1,
      `expected one injected instruction, saw ${host.injected.length}`,
    );
    assert.match(host.injected[0][0], /handoff/);
    assert.equal(
      host.injected[0][1].inject,
      false,
      "the instruction must not echo to the terminal",
    );
    assert.equal(
      await box.hasLedger(),
      true,
      "the real CLI must record the trigger",
    );
    const ledger = await box.ledger();
    assert.equal(ledger.sessions["s-node"].count, 1);
    assert.equal(ledger.sessions["s-node"].harness, "pi");
    assert.equal(ledger.sessions["s-node"].handoff, "herdr-handoff");
    assert.equal(
      host.notifications.length,
      0,
      "a handoff trigger that is working stays quiet",
    );
  },
);

test(
  "comfortable context injects nothing and records nothing",
  { skip },
  async () => {
    const box = await sandbox();
    const host = await runTurn(box, {
      usage: { tokensUsed: 20000, contextWindow: 200000, percent: 10 },
    });
    assert.equal(host.injected.length, 0);
    assert.equal(await box.hasLedger(), false);
  },
);

test("an opted-out operator stays silent", { skip }, async () => {
  const box = await sandbox({ enabled: false });
  const host = await runTurn(box, {
    usage: { tokensUsed: 190000, contextWindow: 200000, percent: 95 },
  });
  assert.equal(host.injected.length, 0);
  assert.equal(await box.hasLedger(), false);
});

test(
  "an unusable usage reading never breaks the session",
  { skip },
  async () => {
    const box = await sandbox();
    const host = await runTurn(box, {
      usage: { tokensUsed: 0, contextWindow: 0, percent: null },
    });
    assert.equal(host.injected.length, 0);
    assert.equal(await box.hasLedger(), false);
  },
);

test(
  "a missing herdr-eng is reported quietly, not thrown",
  { skip },
  async () => {
    const box = await sandbox();
    const host = recordingHost({
      usage: { tokensUsed: 190000, contextWindow: 200000, percent: 95 },
    });
    const mod = await import(`${extension}?case=missing-${Math.random()}`);
    mod.default(host.api);
    process.env.HOME = box.home;
    process.env.XDG_STATE_HOME = box.state;
    process.env.XDG_CONFIG_HOME = join(box.home, ".config");
    process.env.HERDR_ENG_ARGS = JSON.stringify([
      join(box.home, "definitely-not-herdr-eng"),
    ]);
    await host.handlers.get("turn_end")(
      { toolResults: [], textContent: "", messages: [] },
      host.ctx,
    );
    await settle();
    assert.equal(host.injected.length, 0);
    assert.equal(
      host.notifications.length,
      1,
      "the operator must learn the trigger could not be raised",
    );
    assert.match(host.notifications[0][0], /herdr-eng|autopilot/i);
  },
);
