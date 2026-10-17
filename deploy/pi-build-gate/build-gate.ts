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
 *   HERDR_BUILD_GATE_SLOTS=N    max concurrent builds host-wide (default 2)
 *   HERDR_BUILD_GATE_MAX_LOAD=F back off while 1-min load/core exceeds this (default 3.0)
 *   HERDR_BUILD_GATE_WAIT=S     seconds to wait for a slot before running ungated (default 900)
 */

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
		if (!cmd || !shouldGate(cmd)) return;

		const slots = envNum("HERDR_BUILD_GATE_SLOTS", 2);
		const maxLoad = envNum("HERDR_BUILD_GATE_MAX_LOAD", 3.0);
		const wait = envNum("HERDR_BUILD_GATE_WAIT", 900);

		// Wrap the original command using the absolute gate path. Shell
		// operators (`|`, `&&`, redirection) still apply to the gate's output,
		// so pipelines keep working.
		event.input.command =
			`${HERDR_ENG} build-coordinate --slots ${slots} ` +
			`--max-load ${maxLoad} --wait ${wait} --bypass-on-timeout -- ${cmd}`;
	});
}
