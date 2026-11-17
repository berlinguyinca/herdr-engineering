/**
 * Handoff autopilot for pi.
 *
 * On `turn_end` it asks the herdr-eng CLI whether this session should be handed to a fresh pane and,
 * when the answer is yes, injects the handoff instruction as the next user turn — silently, so the
 * operator sees nothing but the agent starting to hand off.
 *
 * This file holds no thresholds. The CLI is the authority (config, opt-out, per-session limits, handoffs
 * already running); the extension only remembers what the CLI last said, so it does not spawn a process
 * on every turn.
 *
 * Why `turn_end`: pi's own summarizer runs inside the same session (`core/compact.js` calls
 * `checkCompaction()` then `compact()` on the live session), so by the time compaction fires the detail
 * is already lost. The turn boundary before it is the last moment the whole conversation still exists —
 * the next turn is the one that would be summarized.
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const HARNESS = "pi";
/** The boundary this trigger knows how to act on; it names itself in every verdict. */
const BOUNDARY = "turn_end";
/** Re-ask the CLI this many evaluations apart, so a mid-session config change is obeyed. */
const POLICY_REFRESH_EVERY = 20;
const CLI_TIMEOUT_MS = 15_000;

type CliResult = { ok: boolean; stdout?: string; reason?: string };

/**
 * How to invoke herdr-eng, resolved the same way the build-gate extension does.
 *
 * `HERDR_ENG_ARGS` is a JSON argv (a venv interpreter and `-m herdr_engineering.cli`) set by
 * `scripts/install.sh`. It is parsed, never evaluated: no shell, no interpolation.
 */
function cliCommand(): { bin: string; prefix: string[] } | null {
	const raw = process.env.HERDR_ENG_ARGS;
	if (raw) {
		try {
			const parsed: unknown = JSON.parse(raw);
			if (
				Array.isArray(parsed) &&
				parsed.length > 0 &&
				parsed.every((part): part is string => typeof part === "string") &&
				parsed[0].length > 0
			) {
				const [bin, ...prefix] = parsed;
				return { bin, prefix };
			}
		} catch {
			// A malformed value is not a reason to stop working; fall through to PATH.
		}
	}
	const bundled = new URL("./bin/herdr-eng", import.meta.url).pathname;
	if (existsSync(bundled)) return { bin: bundled, prefix: [] };
	return { bin: "herdr-eng", prefix: [] };
}

/** Run the CLI. It is authoritative; this extension only remembers what it was told. */
function askCli(args: string[]): CliResult {
	const command = cliCommand();
	if (command === null) {
		return { ok: false, reason: "HERDR_ENG_ARGS is not a JSON array of strings" };
	}
	let result: ReturnType<typeof spawnSync>;
	try {
		result = spawnSync(command.bin, [...command.prefix, ...args], {
			cwd: process.cwd(),
			encoding: "utf8",
			timeout: CLI_TIMEOUT_MS,
			maxBuffer: 1024 * 1024,
		});
	} catch (error) {
		return { ok: false, reason: `could not run ${command.bin}: ${String(error)}` };
	}
	if (result.error) {
		return { ok: false, reason: `could not run ${command.bin}: ${result.error.message}` };
	}
	// spawnSync is typed loosely here: the buffers are text because we asked for utf8.
	const stdout = String(result.stdout ?? "");
	if (result.status !== 0) {
		return {
			ok: false,
			reason: `${command.bin} exited ${result.status}: ${String(result.stderr ?? "").trim().slice(0, 300)}`,
		};
	}
	return { ok: true, stdout };
}

function parseVerdict(stdout: string): Record<string, unknown> | null {
	try {
		const parsed: unknown = JSON.parse(stdout);
		if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return null;
		const record = parsed as Record<string, unknown>;
		if (typeof record.trigger !== "boolean") return null;
		return record;
	} catch {
		return null;
	}
}

/** A context-usage record, read across the pi versions this extension has to survive. */
type UsageLike = {
	percent?: number | null;
	contextWindow?: number;
	tokens?: number | null;
	tokensUsed?: number | null;
};

function rawUsage(ctx: ExtensionContext): UsageLike | null {
	// Read defensively: pi's ContextUsage has changed field names between builds.
	const usage = ctx.getContextUsage?.() as unknown as UsageLike | undefined | null;
	return usage ?? null;
}

/**
 * Percent of the window used, or null when the harness cannot say.
 *
 * `percent` is the answer when a build reports it. Older builds report `{ tokens, contextWindow }` and
 * newer docs `{ tokensUsed, contextWindow }`, so the fallback reads both spellings. Returning null is
 * deliberate: `unknown_context` from the CLI is a better decision than guessing how full a session is.
 */
function usagePercent(usage: UsageLike | null): number | null {
	if (usage === null) return null;
	if (typeof usage.percent === "number" && Number.isFinite(usage.percent)) return usage.percent;
	const used = typeof usage.tokens === "number" ? usage.tokens : usage.tokensUsed;
	const span = usage.contextWindow;
	if (typeof used === "number" && typeof span === "number" && span > 0) {
		return (used / span) * 100;
	}
	return null;
}

/** Tokens used, whichever spelling this build uses; null when it does not say. */
function usageTokens(usage: UsageLike | null): number | null {
	if (usage === null) return null;
	const used = typeof usage.tokens === "number" ? usage.tokens : usage.tokensUsed;
	return typeof used === "number" ? used : null;
}

/**
 * The session id, from whichever accessor this pi build exposes.
 *
 * `ctx.sessionManager.getSessionId()` in current pi, a bare `ctx.sessionId` in older builds. It keys the
 * per-session memo here and the ledger the CLI writes; "" means the host cannot say, and we leave it alone.
 */
function sessionKey(ctx: ExtensionContext): string {
	// Read defensively: `sessionManager.getSessionId()` in current pi, a bare `sessionId` in older builds.
	const host = ctx as unknown as {
		sessionManager?: { getSessionId?: () => string };
		sessionId?: string;
	};
	const fromManager = host.sessionManager?.getSessionId?.();
	if (typeof fromManager === "string" && fromManager) return fromManager;
	return typeof host.sessionId === "string" ? host.sessionId : "";
}

/**
 * `ctx.sendUserMessage` with pi's silent-injection options.
 *
 * pi documents `{ deliver: false, inject: false }`: never wake a running turn, and keep the message out
 * of the TUI transcript while it still enters the model context. Builds that predate those options ignore
 * them and return nothing — which is why only an explicit `false` counts as "not delivered".
 */
type SilentSend = (
	content: string,
	options?: { deliver?: boolean; inject?: boolean },
) => Promise<boolean | void>;

export default function (pi: ExtensionAPI) {
	/** Per session: the last usage reading we asked the CLI about, so an unchanged reading does not
	 *  spawn a process every turn. A memo, not a policy — the CLI decides. */
	const asked = new Map<string, string>();
	/** What the CLI last told us: the threshold it applied and whether it will act at all. Cached from
	 *  verdicts only (`threshold`/`code`), never invented here. */
	let threshold: number | null = null;
	let idleUntilRefresh = false;
	let evaluations = 0;
	/** Sessions already warned about, so a broken install cannot spam the UI. */
	const warned = new Set<string>();
	const warn = (sessionId: string, message: string, ctx: ExtensionContext) => {
		if (warned.has(sessionId)) return;
		warned.add(sessionId);
		ctx.ui?.notify?.(`herdr handoff autopilot: ${message}`, "error");
	};

	pi.on("turn_end", async (event, ctx) => {
		void event;
		let sessionId = "unknown";
		try {
			const usage = rawUsage(ctx);
			const percent = usagePercent(usage);
			if (usage === null) return; // a context that cannot report its size is left alone
			sessionId = sessionKey(ctx);
			if (!sessionId) return;

			const signature = `${percent ?? "none"}/${usageTokens(usage) ?? "none"}`;
			if (asked.get(sessionId) === signature) return; // nothing new to decide on
			asked.set(sessionId, signature);

			// pi awaits extension handlers in stream order, so a subprocess on every turn would slow
			// every turn. A cached threshold from the CLI's own verdict lets us stay quiet while the
			// context is nowhere near the limit; the decision itself is never local.
			evaluations += 1;
			const refresh = evaluations % POLICY_REFRESH_EVERY === 0;
			if (!refresh && idleUntilRefresh) return;
			if (!refresh && threshold !== null && percent !== null && percent < threshold) return;

			const usageArgs: string[] = [];
			if (percent !== null) {
				usageArgs.push("--percent", String(percent));
			} else {
				const tokens = usageTokens(usage);
				if (tokens !== null && tokens > 0) {
					usageArgs.push("--tokens", String(tokens));
					const span = usage?.contextWindow;
					if (typeof span === "number" && span > 0) {
						usageArgs.push("--context-window", String(span));
					}
				}
			}
			const identity = [
				"--session-id",
				sessionId,
				"--harness",
				HARNESS,
				"--boundary",
				BOUNDARY,
				"--cwd",
				process.cwd(),
				"--json",
			];

			const answer = askCli(["handoff", "autopilot", "check", ...identity, ...usageArgs]);
			if (!answer.ok) {
				warn(sessionId, answer.reason ?? "the handoff autopilot could not be reached", ctx);
				return;
			}
			const verdict = parseVerdict(answer.stdout ?? "");
			if (verdict === null) {
				warn(sessionId, "herdr-eng returned no verdict; install or upgrade herdr-eng", ctx);
				return;
			}
			if (typeof verdict.threshold === "number") threshold = verdict.threshold;
			const code = typeof verdict.code === "string" ? verdict.code : "";
			// Disabled, opted out, unsupported, already told, or a handoff already running: nothing more
			// to do until the next refresh — and the CLI said so, not us.
			idleUntilRefresh =
				verdict.trigger !== true &&
				["disabled", "harness_unsupported", "already_triggered", "handoff_underway"].includes(code);
			if (verdict.trigger !== true) return;

			const prompt = typeof verdict.prompt === "string" ? verdict.prompt : "";
			if (!prompt) {
				warn(sessionId, "the verdict said trigger but carried no instruction", ctx);
				return;
			}
			const send = pi.sendUserMessage as unknown as SilentSend;
			const injected = await send(prompt, { deliver: false, inject: false });
			if (injected === false) {
				// Not delivered, so not recorded: the next boundary re-arms the trigger.
				warn(sessionId, "pi refused the handoff instruction, so it was not recorded", ctx);
				return;
			}

			const recorded = askCli(["handoff", "autopilot", "record", ...identity, ...usageArgs]);
			if (!recorded.ok) {
				// The instruction landed; losing the ledger entry must not undo that.
				warn(sessionId, `the trigger was injected but not recorded: ${recorded.reason}`, ctx);
			}
		} catch (error) {
			// A handoff trigger must never be able to break the session it is protecting.
			warn(sessionId, `unexpected failure: ${String(error)}`, ctx);
		}
	});
}
