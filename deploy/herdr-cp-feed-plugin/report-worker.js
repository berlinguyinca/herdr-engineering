// Runs full /summary reports for this host's agents, on request from the control plane.
//
// The control plane runs in a container with no session records and no summarizer, so it only queues
// requests. This worker (inside the existing session service) claims at most one job at a time for its own
// host, runs `herdr-eng summary report` here (where the records and the CLI credentials live), and posts the
// result back. It reaches only the control plane URL it already uses; argv never passes through a shell.
import { execFile } from "node:child_process";
import { homedir } from "node:os";

export const REPORT_POLL_MS = 5000;
const AGENTS = new Set(["claude", "pi", "codex", "opencode"]);
const HARNESSES = new Set(["claude"]);
const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const MAX_OUTPUT = 4 * 1024 * 1024;

function toolPath(env) {
  const home = env.HOME || homedir();
  return [`${home}/.local/bin`, "/opt/homebrew/bin", "/usr/local/bin", env.PATH || "/usr/bin:/bin"].join(":");
}

// Resolves to the report command's JSON ({ok, report, harness, duration_s} or {ok:false, error, detail}).
export function runReport(job, { execFileFn = execFile, env = process.env } = {}) {
  const timeout = Number.isInteger(job?.timeout_seconds) ? Math.min(Math.max(job.timeout_seconds, 10), 900) : 300;
  if (!AGENTS.has(job?.agent) || typeof job?.target !== "string" || !SESSION_ID.test(job.target) || !HARNESSES.has(job?.harness)) {
    return Promise.resolve({ ok: false, error: "invalid_session_id" });
  }
  const args = ["summary", "report", "--agent", job.agent, "--session", job.target, "--harness", job.harness, "--timeout", String(timeout)];
  return new Promise(resolve => {
    execFileFn("herdr-eng", args, { timeout: (timeout + 30) * 1000, maxBuffer: MAX_OUTPUT, env: { ...env, PATH: toolPath(env) } },
      (error, stdout) => {
        let parsed = null;
        try { parsed = JSON.parse(String(stdout || "")); } catch { /* not JSON: handled below */ }
        if (parsed && typeof parsed === "object") return resolve(parsed);
        if (error?.killed) return resolve({ ok: false, error: "timeout" });
        resolve({ ok: false, error: error?.code === "ENOENT" ? "harness_missing" : "summarizer_failed" });
      });
  });
}

export function createReportWorker({ fetcher = fetch, run = runReport, hostId, sleep = ms => new Promise(r => setTimeout(r, ms)) } = {}) {
  let busy = false;
  const post = async ({ url, apiKey }, path, body) => {
    const headers = { "Content-Type": "application/json" };
    if (apiKey) headers["X-API-Key"] = apiKey;
    const response = await fetcher(`${url}/api/v1/host/summary-reports/${path}`, {
      method: "POST", headers, body: JSON.stringify(body), redirect: "error", signal: AbortSignal.timeout(15000),
    });
    return response;
  };
  // One pass: claim, run, report. Returns what happened, never throws.
  return async function tick(target) {
    if (busy || !target?.url || !hostId) return { ran: false };
    let jobs;
    try {
      const response = await post(target, "claim", { host_id: hostId });
      if (!response.ok) { await response.body?.cancel(); return { ran: false, status: response.status }; }
      jobs = (await response.json())?.jobs;
    } catch { return { ran: false }; }
    const job = Array.isArray(jobs) ? jobs[0] : null;
    if (!job?.operation_id || !job?.session_id) return { ran: false };
    busy = true;
    try {
      const result = await run(job).catch(() => ({ ok: false, error: "summarizer_failed" }));
      const body = result?.ok === true
        ? { ok: true, report: result.report, harness: result.harness, duration_s: result.duration_s }
        : { ok: false, error: result?.error, detail: typeof result?.detail === "string" ? result.detail.slice(0, 300) : "" };
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const response = await post(target, "result", { ...body, host_id: hostId, session_id: job.session_id, operation_id: job.operation_id });
          await response.body?.cancel();
          if (response.ok) return { ran: true, ok: body.ok };
          if (response.status >= 400 && response.status < 500) return { ran: true, ok: body.ok, rejected: response.status };
        } catch { /* retry */ }
        await sleep(2000 * (attempt + 1));
      }
      return { ran: true, ok: body.ok, undelivered: true };
    } finally { busy = false; }
  };
}
