import { appContract } from "./src/packages/contracts/src/rpc.ts";
import { z } from "./src/packages/contracts/node_modules/zod/index.js";
import { writeFileSync } from "node:fs";
const out: Record<string, unknown> = {};
const failed: string[] = [];
function walk(node: any, path: string[]) {
  const def = node?.["~orpc"];
  if (def && ("inputSchema" in def || "outputSchema" in def || "route" in def)) {
    const name = path.join("/");
    const conv = (s: any) => { if (!s) return null; try { return z.toJSONSchema(s, { unrepresentable: "any", io: "output" }); } catch (e) { failed.push(name); return null; } };
    out[name] = { input: conv(def.inputSchema), output: conv(def.outputSchema) };
    return;
  }
  if (node && typeof node === "object") for (const [k, v] of Object.entries(node)) walk(v, [...path, k]);
}
walk(appContract, []);
writeFileSync(process.argv[2] ?? "contract-schemas.json", JSON.stringify({ upstream: "elie222/rakazo", sha: process.argv[3] ?? "unknown", procedures: out }));
console.log(Object.keys(out).length, "procedures; failed:", [...new Set(failed)].join(","));
