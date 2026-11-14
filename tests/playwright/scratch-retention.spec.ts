import { test, expect } from "playwright/test";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import {
  mkdtemp,
  mkdir,
  writeFile,
  readFile,
  rm,
  access,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const run = promisify(execFile);

function readReport(text: string): {
  removed: number;
  planned: number;
  protected: number;
  dry: boolean;
} {
  const value: unknown = JSON.parse(text);
  if (typeof value !== "object" || value === null)
    throw new Error("missing report");
  const data = value as Record<string, unknown>;
  if (
    typeof data.removed_count !== "number" ||
    typeof data.would_remove_count !== "number" ||
    typeof data.protected_count !== "number" ||
    typeof data.dry_run !== "boolean"
  )
    throw new Error("invalid report counters");
  return {
    removed: data.removed_count,
    planned: data.would_remove_count,
    protected: data.protected_count,
    dry: data.dry_run,
  };
}

test("public cleanup CLI reclaims native compiler output and preserves source and keep markers", async () => {
  const python = process.env.SCRATCH_TEST_PYTHON || "python3";
  const uid = process.getuid?.();
  expect(uid).toBeDefined();
  const temporary = await mkdtemp(path.join(tmpdir(), "herdr-scratch-e2e-"));
  try {
    const root = path.join(temporary, `claude-${uid}`);
    const session = path.join(
      root,
      "-fixture",
      "01234567-89ab-4cde-8fab-0123456789ab",
    );
    const project = path.join(session, "scratchpad", "native-project");
    const target = path.join(project, "target");
    const source = path.join(project, "src", "lib.rs");
    const logs = path.join(temporary, "empty-session-logs");
    await mkdir(path.dirname(source), { recursive: true });
    await mkdir(logs);
    await writeFile(
      path.join(project, "Cargo.toml"),
      '[package]\nname="retention_cli_proof"\nversion="0.1.0"\nedition="2021"\n',
    );
    await writeFile(source, "pub fn answer() -> u32 { 42 }\n");
    const cargo = (await run("rustup", ["which", "cargo"])).stdout.trim();
    const compilerArgs = [
      "check",
      "--offline",
      "--manifest-path",
      path.join(project, "Cargo.toml"),
      "--target-dir",
      target,
    ];
    const compilerEnv = {
      ...process.env,
      RUSTC_WRAPPER: "",
      RUSTC_WORKSPACE_WRAPPER: "",
    };
    await run(cargo, compilerArgs, { env: compilerEnv });
    const expected = "pub fn answer() -> u32 { 43 } // unique source edit\n";
    await writeFile(source, expected);
    const cli = [
      "-I",
      "-m",
      "herdr_engineering.scratch_reaper",
      "--root",
      root,
      "--quarantine",
      path.join(temporary, "quarantine"),
      "--state-dir",
      path.join(temporary, "state"),
      "--gate-state-dir",
      path.join(temporary, "gate"),
      "--session-root",
      logs,
      "--ttl-minutes",
      "0",
      "--min-free-gib",
      "0",
    ];
    const dry = readReport(
      (await run(python, [...cli, "--apply", "--dry-run"])).stdout,
    );
    expect(dry.dry).toBe(true);
    expect(dry.planned).toBe(1);
    expect(dry.removed).toBe(0);
    await access(target);
    const applied = readReport((await run(python, [...cli, "--apply"])).stdout);
    expect(applied.removed).toBe(1);
    expect(await readFile(source, "utf8")).toBe(expected);
    await expect(access(target)).rejects.toThrow();
    const repeated = readReport(
      (await run(python, [...cli, "--apply"])).stdout,
    );
    expect(repeated.removed).toBe(0);
    await run(cargo, compilerArgs, { env: compilerEnv });
    await writeFile(path.join(session, ".keep"), "");
    const retained = readReport(
      (await run(python, [...cli, "--apply"])).stdout,
    );
    expect(retained.removed).toBe(0);
    expect(retained.protected).toBeGreaterThan(0);
    await access(target);
    expect(await readFile(source, "utf8")).toBe(expected);
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});
