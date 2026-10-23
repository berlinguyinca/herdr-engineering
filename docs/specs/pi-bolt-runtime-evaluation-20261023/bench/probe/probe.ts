import { writeFileSync } from "node:fs";
export default function (pi: any) {
  const out = process.env.PROBE_OUT!;
  const snap = (why: string) => {
    const g = globalThis as any;
    let cmds: string[] = [];
    try { cmds = (pi.getCommands?.() ?? []).map((c: any) => c.name); } catch (e) { cmds = ["getCommands-error:" + e]; }
    writeFileSync(out, JSON.stringify({ why, bun: !!(process.versions as any).bun, node: process.versions.node, fallback: g[Symbol.for("pi-engineering.host-fallback.v1")] ?? null, commands: cmds.sort() }) + "\n");
  };
  pi.on?.("session_start", () => { setTimeout(() => snap("session_start+3s"), 3000); });
  setTimeout(() => snap("factory+6s"), 6000).unref?.();
  process.on("exit", () => snap("exit"));
}
