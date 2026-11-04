// Spec 08: Pi build-gate extension. Imports the real .ts (Node type stripping);
// the only stand-in is the Pi host (`pi.on`) and a recording `herdr-eng` script.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const skip = process.features.typescript ? false : "node without TypeScript type stripping";
const TS = path.resolve("deploy/pi-build-gate/build-gate.ts");

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-gate-"));
const log = path.join(dir, "argv.log");
const fake = path.join(dir, "herdr-eng");
const NEW_HELP = "usage: herdr-eng build-coordinate [--record] [--require-cache-enabled] [--trace-session ID] [--trace-mission ID]";
const OLD_HELP = "usage: herdr-eng build-coordinate [--slots N] [--max-load F] [--record]";
// A real executable that answers `build-coordinate --help` like a given CLI version and otherwise records argv.
function writeStub(file, help, exitCode = 0) {
  fs.writeFileSync(file, `#!/bin/sh\ncase " $* " in *" --help "*) echo '${help}'; exit ${exitCode};; esac\n` +
    `for a in "$@"; do printf '%s\\n' "$a"; done > "${log}"\n`, { mode: 0o755 });
}
writeStub(fake, NEW_HELP);
process.env.PATH = `${dir}${path.delimiter}${process.env.PATH}`;
delete process.env.HERDR_BUILD_GATE_DISABLE;

const mod = skip ? null : await import(pathToFileURL(TS).href);

function handlerFor() {
  let handler;
  mod.default({ on: (_name, fn) => { handler = fn; } });
  return (command) => {
    const event = { type: "tool_call", toolName: "bash", input: { command } };
    handler(event);
    return event.input.command;
  };
}

test("simple parser accepts plain commands only", { skip }, () => {
  assert.deepEqual(mod.parseSimpleCommand("cargo build -p foo --features=a,b"),
    ["cargo", "build", "-p", "foo", "--features=a,b"]);
  for (const bad of [
    "cargo build | tail", "cargo build && ls", "cargo build; ls", "(cargo build)",
    "cargo build > out", "cargo build < in", "cargo build $(id)", "cargo build `id`",
    "FOO=1 cargo build", "cargo build 'x y'", 'cargo build "x"', "cargo build \\\n -p x",
    "cargo build *", "cargo build ~", "cargo build &", "cargo build\nls", "cargo build $HOME", "",
    "cargo build {a,b}", "cargo build !x", "cargo build #c",
  ]) assert.equal(mod.parseSimpleCommand(bad), null, JSON.stringify(bad));
});

test("kind classification", { skip }, () => {
  const k = (c) => mod.simpleKind(mod.parseSimpleCommand(c));
  assert.equal(k("cargo build"), "cargo build");
  assert.equal(k("cargo +nightly test -p x"), "cargo test");
  assert.equal(k("cargo check"), "cargo check");
  assert.equal(k("cargo clippy"), "cargo clippy");
  assert.equal(k("cargo nextest run"), "cargo nextest");
  assert.equal(k("rustc main.rs"), "rustc");
  assert.equal(k("cargo fmt"), null);
  assert.equal(k("cargo run"), null);
  assert.equal(k("make"), null);
});

test("already coordinated commands are left alone", { skip }, () => {
  const none = {};
  assert.equal(mod.alreadyCoordinated("herdr-eng build-coordinate -- cargo build", none), true);
  assert.equal(mod.alreadyCoordinated("/x/herdr-eng build-coordinate --slots 2 -- cargo test", none), true);
  assert.equal(mod.alreadyCoordinated("sccache rustc main.rs", none), true);
  assert.equal(mod.alreadyCoordinated("RUSTC_WRAPPER=sccache cargo build", none), true);
  assert.equal(mod.alreadyCoordinated("cargo build", { RUSTC_WRAPPER: "sccache" }), true);
  assert.equal(mod.alreadyCoordinated("cargo build", { CARGO_BUILD_RUSTC_WRAPPER: "x" }), true);
  assert.equal(mod.alreadyCoordinated("cargo build", { RUSTC_WORKSPACE_WRAPPER: "x" }), true);
  assert.equal(mod.alreadyCoordinated("cargo build", { RUSTC_WRAPPER: "" }), false);
  assert.equal(mod.alreadyCoordinated("cargo build", none), false);
});

test("trace args: ids only, validated, no secrets", { skip }, () => {
  assert.deepEqual(mod.traceArgs({ session: "s-1", mission: "m_2" }),
    ["--trace-session", "s-1", "--trace-mission", "m_2"]);
  assert.deepEqual(mod.traceArgs({ session: "s-1", mission: "bad value" }), ["--trace-session", "s-1"]);
  assert.deepEqual(mod.traceArgs({ session: "ghp_abcdefghijklmnop" }), []);
  assert.deepEqual(mod.traceArgs({ session: "my-token-1", mission: "m" }), []);
  assert.deepEqual(mod.traceArgs({ session: "a".repeat(65) }), []);
  assert.deepEqual(mod.traceArgs({ session: undefined, mission: "m" }), []);
});

test("existing heavy rewrite is unchanged (no trace, no record)", { skip }, () => {
  const rewrite = handlerFor();
  const out = rewrite("cargo check --workspace --tests");
  assert.equal(out, `${fake} build-coordinate --max-load 3 --wait 900 --bypass-on-timeout -- cargo check --workspace --tests`);
  assert.equal(rewrite("cargo fmt"), "cargo fmt");
  assert.equal(rewrite("ls -la"), "ls -la");
});

test("light simple cargo/rustc commands are wrapped, require cache enabled, carry trace", { skip }, () => {
  process.env.HERDR_SESSION_ID = "sess-77";
  process.env.HERDR_MISSION_ID = "mission-5";
  const out = handlerFor()("cargo nextest run -p foo");
  delete process.env.HERDR_SESSION_ID;
  delete process.env.HERDR_MISSION_ID;
  assert.equal(out, `${fake} build-coordinate --max-load 3 --wait 5 --bypass-on-timeout --record ` +
    `--require-cache-enabled --trace-session sess-77 --trace-mission mission-5 -- cargo nextest run -p foo`);
});

test("session falls back to a per-process id; secret-looking env ids are dropped", { skip }, () => {
  delete process.env.HERDR_SESSION_ID;
  process.env.HERDR_MISSION_ID = "tok-secret-1";
  const rewrite = handlerFor();
  const out = rewrite("rustc main.rs");
  delete process.env.HERDR_MISSION_ID;
  assert.match(out, /--trace-session [0-9a-f-]{36} -- rustc main\.rs$/);
  assert.ok(!out.includes("--trace-mission"));
});

test("compound, env-prefixed and already wrapped light commands are untouched", { skip }, () => {
  const rewrite = handlerFor();
  for (const c of [
    "cargo build -p x | tail", "cargo test -p x && echo ok", "cargo test -p x; ls", "(cargo check -p x)",
    "cargo build -p x > out.txt", "cargo build -p $(echo x)", "FOO=1 cargo build -p x",
    "RUSTC_WRAPPER=sccache cargo build -p x", "sccache rustc a.rs", "cargo build -p 'a b'",
    "herdr-eng build-coordinate -- cargo build -p x",
  ]) assert.equal(rewrite(c), c, c);
  // a heavy command that is already gated is not double wrapped either
  const g = "herdr-eng build-coordinate -- cargo check --workspace";
  assert.equal(rewrite(g), g);
});

test("user RUSTC_WRAPPER in the process env suppresses the light and heavy wrap", { skip }, () => {
  process.env.RUSTC_WRAPPER = "sccache";
  try {
    const rewrite = handlerFor();
    assert.equal(rewrite("cargo build -p x"), "cargo build -p x");
  } finally { delete process.env.RUSTC_WRAPPER; }
});

test("the rewritten command runs through a real shell with the original argv intact", { skip }, () => {
  process.env.HERDR_SESSION_ID = "sess-9";
  const out = handlerFor()("cargo test -p foo --features=a,b");
  delete process.env.HERDR_SESSION_ID;
  execFileSync("sh", ["-c", out]);
  const argv = fs.readFileSync(log, "utf8").trim().split("\n");
  assert.deepEqual(argv.slice(-6), ["--", "cargo", "test", "-p", "foo", "--features=a,b"]);
  assert.ok(argv.includes("--require-cache-enabled") && argv.includes("sess-9"));
});

test("disable switch still wins", { skip }, () => {
  process.env.HERDR_BUILD_GATE_DISABLE = "1";
  try { assert.equal(handlerFor()("cargo build -p x"), "cargo build -p x"); }
  finally { delete process.env.HERDR_BUILD_GATE_DISABLE; }
});

// Load a fresh copy of the extension with `herdr-eng` stubbed as the given CLI version.
async function loadWith(help, exitCode = 0) {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "pi-gate-skew-"));
  const bin = path.join(d, "herdr-eng");
  writeStub(bin, help, exitCode);
  const saved = process.env.PATH;
  process.env.PATH = `${d}${path.delimiter}${saved}`;
  try {
    const m = await import(`${pathToFileURL(TS).href}?skew=${d}`);
    let handler;
    m.default({ on: (_n, fn) => { handler = fn; } });
    return { bin, run: (command) => { const e = { type: "tool_call", toolName: "bash", input: { command } }; handler(e); return e.input.command; } };
  } finally { process.env.PATH = saved; }
}

test("old herdr-eng (no new flags): light path skipped, heavy path byte-identical", { skip }, async () => {
  const { bin, run } = await loadWith(OLD_HELP);
  assert.equal(run("cargo build -p x"), "cargo build -p x");
  assert.equal(run("rustc main.rs"), "rustc main.rs");
  assert.equal(run("cargo check --workspace --tests"),
    `${bin} build-coordinate --max-load 3 --wait 900 --bypass-on-timeout -- cargo check --workspace --tests`);
});

test("failing or missing probe also disables only the light path", { skip }, async () => {
  const { run } = await loadWith(NEW_HELP, 2);
  assert.equal(run("cargo test -p x"), "cargo test -p x");
});

test("new herdr-eng: light path enabled", { skip }, async () => {
  const { bin, run } = await loadWith(NEW_HELP);
  assert.match(run("cargo test -p x"), new RegExp(`^${bin.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} build-coordinate .*--require-cache-enabled`));
});

test("a herdr-eng path with spaces is shell-quoted; slots must be digits", { skip }, () => {
  assert.equal(mod.shellQuote("/a b/herdr-eng"), "'/a b/herdr-eng'");
  assert.equal(mod.shellQuote("/usr/bin/herdr-eng"), "/usr/bin/herdr-eng");
  process.env.HERDR_BUILD_GATE_SLOTS = "\n5";
  try { assert.ok(!handlerFor()("cargo check --workspace").includes("--slots")); }
  finally { delete process.env.HERDR_BUILD_GATE_SLOTS; }
});
