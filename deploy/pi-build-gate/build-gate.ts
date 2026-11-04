/**
 * Build-Gate Extension — cross-agent CPU build-storm prevention.
 *
 * Autonomous pi sessions (and the subagents/workers they spawn) frequently fire
 * full-workspace `cargo check --workspace --tests` / `tsc --noEmit` builds
 * independently. With no shared gate, N agents launch N×`-j` compiler jobs at
 * once and CPU-starve the host (observed: 24 cores, load 27→46, the dev-fabric
 * gateway and the machine itself became unresponsive).
 *
 * This extension transparently routes heavy build commands through the per-host
 * build gate provided by `herdr-eng build-coordinate`, so all pi sessions on a
 * host contend for a bounded number of build slots. It does NOT change light,
 * single-crate checks, and it does NOT affect anything outside the pi agent.
 *
 * Safety: the gate binary is resolved once at load time by scanning PATH. If
 * `herdr-eng` is not found, gating is disabled and every build passes through
 * unchanged — the extension must NEVER break a build, only coordinate it.
 *
 * Config (env):
 *   HERDR_BUILD_GATE_DISABLE=1  disable gating entirely
 *   HERDR_BUILD_GATE_SLOTS=N    max concurrent builds host-wide (default: auto =
 *                               max(2, cpu_cores-4); omit to let herdr-eng choose)
 *   HERDR_BUILD_GATE_MAX_LOAD=F back off while 1-min load/core exceeds this (default 3.0)
 *   HERDR_BUILD_GATE_WAIT=S     seconds to wait for a slot before running ungated (default 900)
 *
 * Spec 08 (fabric build cache): light, SIMPLE `cargo build|check|test|clippy|
 * nextest` and `rustc` commands are also routed through `herdr-eng
 * build-coordinate --record --require-cache-enabled`. The extension cannot read
 * the YAML config, so herdr-eng decides: with `fabric_build_cache` disabled it
 * just runs the original command (no slot, no events). A command counts as
 * simple only if it matches a conservative character allowlist (no quotes, no
 * `| & ; < > ( ) $ \` ~ * ! # { }`, no newline, no leading VAR=value); anything
 * else is passed through unchanged (fail open to the original command, as
 * before). No shell parsing is attempted. Commands already routed through
 * build-coordinate/sccache, or run while RUSTC_WRAPPER-style variables are set,
 * are never wrapped again. Each wrapped call carries a non-secret trace (session
 * id from HERDR_SESSION_ID or a per-process id, optional HERDR_MISSION_ID);
 * argv and env values are never sent in it.
 */

import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Heavy cargo subcommands: full-workspace / all-targets / --tests checks,
// builds, clippy and tests are the storm source. Single-crate `-p foo` checks
// are left ungated so ordinary iteration latency is unaffected.
const HEAVY_CARGO = /^cargo\s+(check|build|test|clippy)\b/;
const HEAVY_CARGO_FLAGS = /--workspace|--all-targets|--tests|--all-features/;

// Any tsc invocation on a large project can be heavy; gate them all.
const HEAVY_TSC = /^(?:npx\s+)?tsc\b/;

/** Absolute path to `herdr-eng` found on PATH, or null if unavailable. */
const HERDR_ENG: string | null = (() => {
	try {
		const dirs = (process.env.PATH ?? "").split(path.delimiter).filter(Boolean);
		for (const dir of dirs) {
			const candidate = path.join(dir, "herdr-eng");
			try {
				fs.accessSync(candidate, fs.constants.X_OK);
				return candidate;
			} catch {
				/* keep searching */
			}
		}
	} catch {
		/* fall through to null */
	}
	return null;
})();

function envNum(name: string, fallback: number): number {

	const raw = process.env[name];
	if (raw === undefined || raw === "") return fallback;
	const n = Number(raw);
	return Number.isFinite(n) ? n : fallback;
}

// Slots are adaptive in herdr-eng (`--slots` auto = max(2, cores-4)), so we
// only pin them when the operator explicitly set HERDR_BUILD_GATE_SLOTS;
// otherwise omit the flag and let herdr-eng scale with the host's core count.
function slotsFlag(): string {

	const raw = process.env.HERDR_BUILD_GATE_SLOTS;
	if (raw === undefined || raw === "" || !Number.isFinite(Number(raw))) return "";
	return ` --slots ${raw}`;
}

// ---- spec 08: simple-command coverage, double-wrap guard, trace ----------------

const SIMPLE_TOKEN = /^[A-Za-z0-9_./:@%+=,-]+$/;
const LIGHT_CARGO = new Set(["build", "check", "test", "clippy", "nextest"]);
const WRAPPER_VARS = ["RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_BUILD_RUSTC_WRAPPER"];
const TRACE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
const SECRETISH = ["token", "secret", "passw", "key", "cred", "auth", "bearer"];
const SECRET_PREFIXES = ["sk-", "ghp_", "gho_", "ghs_", "github_pat", "xox", "akia", "asia", "eyj"];

export type CmdKind = "cargo build" | "cargo check" | "cargo test" | "cargo clippy" | "cargo nextest" | "rustc";
export interface Trace {
	session?: string | undefined;
	mission?: string | undefined;
}

/** Tokens of a plain command, or null if there is ANY shell syntax to worry about. */
export function parseSimpleCommand(command: string): string[] | null {
	if (command.trim() === "" || /[\n\r]/.test(command)) return null;
	const tokens = command.trim().split(/[ \t]+/);
	if (!tokens.every((t) => SIMPLE_TOKEN.test(t))) return null;
	const first = tokens[0] ?? "";
	if (first.includes("=")) return null; // env-assignment prefix
	return tokens;
}

/** Allowlisted kind of an already-simple command, or null. */
export function simpleKind(tokens: string[] | null): CmdKind | null {
	if (tokens === null || tokens.length === 0) return null;
	const prog = path.basename(tokens[0] ?? "");
	if (prog === "rustc") return "rustc";
	if (prog !== "cargo") return null;
	const sub = tokens.slice(1).find((t) => !t.startsWith("+"));
	if (sub === undefined || !LIGHT_CARGO.has(sub)) return null;
	return `cargo ${sub}` as CmdKind;
}

/** True when the command is already gated/cached or the user set a rustc wrapper. */
export function alreadyCoordinated(command: string, env: Record<string, string | undefined>): boolean {
	if (/\bbuild-coordinate\b|\bsccache\b/.test(command)) return true;
	if (WRAPPER_VARS.some((v) => command.includes(`${v}=`))) return true;
	return WRAPPER_VARS.some((v) => (env[v] ?? "") !== "");
}

function validTraceId(value: string | undefined): value is string {
	if (value === undefined || !TRACE_ID.test(value)) return false;
	const low = value.toLowerCase();
	if (SECRET_PREFIXES.some((p) => low.startsWith(p)) || SECRETISH.some((w) => low.includes(w))) return false;
	return !(value.length >= 40 && /^[A-Za-z0-9]+$/.test(value));
}

/** CLI args carrying ids only; empty when the session id is missing or invalid. */
export function traceArgs(trace: Trace): string[] {
	if (!validTraceId(trace.session)) return [];
	const args = ["--trace-session", trace.session];
	if (validTraceId(trace.mission)) args.push("--trace-mission", trace.mission);
	return args;
}

const PROCESS_SESSION = randomUUID();

function currentTrace(): Trace {
	const s = process.env.HERDR_SESSION_ID;
	return { session: s !== undefined && s !== "" ? s : PROCESS_SESSION, mission: process.env.HERDR_MISSION_ID };
}

function shouldGate(command: string): boolean {
	const c = command.trim();
	if (HEAVY_CARGO.test(c) && HEAVY_CARGO_FLAGS.test(c)) return true;
	if (HEAVY_TSC.test(c)) return true;
	return false;
}

export default function (pi: ExtensionAPI) {
	// The gate binary must exist or we must not rewrite commands (rewriting to
	// a missing binary would BREAK builds). Resolved at load time.
	if (HERDR_ENG === null) {
		console.warn(
			"[build-gate] 'herdr-eng' not found on PATH; build gating DISABLED " +
			"(heavy builds pass through ungated). Install herdr-eng and restart pi.",
		);
		return; // don't register the handler; no-op extension
	}

	pi.on("tool_call", (event) => {
		if (event.type !== "tool_call" || event.toolName !== "bash") return;
		if (process.env.HERDR_BUILD_GATE_DISABLE === "1") return;

		const cmd = event.input.command as string | undefined;
		if (!cmd) return;
		if (alreadyCoordinated(cmd, process.env)) return; // never double-wrap

		const maxLoad = envNum("HERDR_BUILD_GATE_MAX_LOAD", 3.0);
		const wait = envNum("HERDR_BUILD_GATE_WAIT", 900);

		if (!shouldGate(cmd)) {
			if (simpleKind(parseSimpleCommand(cmd)) === null) return;
			// Light simple build: herdr-eng coordinates only if fabric_build_cache
			// is enabled, otherwise it execs the original command unchanged.
			const trace = traceArgs(currentTrace()).join(" ");
			event.input.command =
				`${HERDR_ENG} build-coordinate${slotsFlag()} ` +
				`--max-load ${maxLoad} --wait ${wait} --bypass-on-timeout --record ` +
				`--require-cache-enabled${trace === "" ? "" : ` ${trace}`} -- ${cmd}`;
			return;
		}

		// Wrap the original command using the absolute gate path. Shell
		// operators (`|`, `&&`, redirection) still apply to the gate's output,
		// so pipelines keep working. --slots is omitted unless the operator
		// pinned HERDR_BUILD_GATE_SLOTS, so herdr-eng scales slots with cores.
		event.input.command =
			`${HERDR_ENG} build-coordinate${slotsFlag()} ` +
			`--max-load ${maxLoad} --wait ${wait} --bypass-on-timeout -- ${cmd}`;
	});
}
