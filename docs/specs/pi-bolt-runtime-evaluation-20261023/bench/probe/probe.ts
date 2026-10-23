// Records what an extension set registered, then exits. Used by `pi-runtime-bench compat`.
// Snapshots inside session_start (fresh ctx) and again on a timer; a good snapshot is never replaced by an error one.
import { writeFileSync } from "node:fs";
export default function (pi: any) {
  const out = process.env.PROBE_OUT!;
  let good = false;
  const names = (xs: any) => (Array.isArray(xs) ? xs.map((x: any) => x?.name ?? String(x)).sort() : []);
  const snap = (why: string) => {
    const g = globalThis as any;
    let commands: string[] = [], tools: string[] = [], err = "";
    try { commands = names(pi.getCommands?.()); } catch (e) { err += `getCommands:${e};`; }
    try { tools = names(pi.getAllTools?.()); } catch (e) { err += `getAllTools:${e};`; }
    if (good && err) return;
    if (!err) good = true;
    writeFileSync(out, JSON.stringify({ why, bun: !!(process.versions as any).bun, node: process.versions.node,
      fallback: g[Symbol.for("pi-engineering.host-fallback.v1")] ?? null, commands, tools, err: err.slice(0, 160) }) + "\n");
  };
  pi.on?.("session_start", () => { snap("session_start"); });
  setTimeout(() => { snap("factory+5s"); if (process.env.PROBE_EXIT) process.exit(0); }, 5000);
}
