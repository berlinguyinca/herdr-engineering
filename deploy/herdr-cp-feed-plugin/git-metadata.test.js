import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { canonicalRepository, createGitMetadataReader } from "./git-metadata.js";
import { enrichSession, resolveSessionEnv } from "./enrich.js";
import { mapEvent } from "./emit.js";

function repository(t, remote = "https://user:secret@GitHub.com/Team/App.git?token=secret") {
  const cwd = mkdtempSync(join(tmpdir(), "herdr-git-metadata-"));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  const git = (...args) => execFileSync("git", ["-C", cwd, ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  git("init");
  git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "initial");
  git("remote", "add", "origin", remote);
  return { cwd, git, sha: git("rev-parse", "HEAD") };
}

test("canonical origins remove credentials and retain distinct forge ports", () => {
  assert.equal(canonicalRepository("https://u:secret@GitHub.com/TEAM/Repo.git?token=secret#private"), "github.com/team/repo");
  assert.equal(canonicalRepository("git@github.com:TEAM/Repo.git"), "github.com/team/repo");
  assert.equal(canonicalRepository("ssh://git@forge.example:2222/Team/Repo.git"), "forge.example:2222/Team/Repo");
  assert.equal(canonicalRepository("ssh://git@forge.example:22/Team/Repo.git"), "forge.example/Team/Repo");
  for (const value of ["/private/repo", "owner/repo", "file:///private/repo", "https://forge.example/a/../b", "https://forge.example/a/%2E%2E/b", "https://forge.example/a/b\nsecret"]) {
    assert.equal(canonicalRepository(value), null);
  }
});

test("local committed checkout emits only canonical origin and full commit", async t => {
  const { cwd, sha } = repository(t);
  assert.deepEqual(await createGitMetadataReader()(cwd), { repository_id: "github.com/team/app", commit_sha: sha });
});

test("TTL cache refreshes changed HEAD and origin without exposing mutable cache", async t => {
  const { cwd, git, sha } = repository(t);
  let now = 0;
  const read = createGitMetadataReader({ now: () => now });
  const first = await read(cwd);
  first.commit_sha = "mutated";
  git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "next");
  git("remote", "set-url", "origin", "git@github.com:team/other.git");
  assert.deepEqual(await read(cwd), { repository_id: "github.com/team/app", commit_sha: sha });
  now = 30000;
  assert.deepEqual(await read(cwd), { repository_id: "github.com/team/other", commit_sha: git("rev-parse", "HEAD") });
});

test("cache evicts old directories at capacity", async t => {
  const a = repository(t), b = repository(t);
  const read = createGitMetadataReader({ maxEntries: 1 });
  await read(a.cwd);
  a.git("remote", "set-url", "origin", "git@github.com:team/replaced.git");
  await read(b.cwd);
  assert.equal((await read(a.cwd)).repository_id, "github.com/team/replaced");
});

test("inherited Git worktree overrides cannot supply another repository's identity", async t => {
  const a = repository(t), b = repository(t, "git@github.com:wrong/repository.git");
  const previous = process.env.GIT_DIR;
  process.env.GIT_DIR = join(b.cwd, ".git");
  try {
    assert.deepEqual(await createGitMetadataReader()(a.cwd), {
      repository_id: "github.com/team/app", commit_sha: a.sha,
    });
  } finally {
    if (previous === undefined) delete process.env.GIT_DIR;
    else process.env.GIT_DIR = previous;
  }
});

test("missing Git executable yields no evidence without failing enrichment", async t => {
  const { cwd } = repository(t);
  const previous = process.env.PATH;
  process.env.PATH = cwd;
  try { assert.deepEqual(await createGitMetadataReader()(cwd), {}); }
  finally {
    if (previous === undefined) delete process.env.PATH;
    else process.env.PATH = previous;
  }
});

test("missing, non-repository, unsupported origin and unborn checkouts provide no evidence", async t => {
  const read = createGitMetadataReader();
  const { cwd, git } = repository(t, "/private/remote.git");
  assert.deepEqual(await read(cwd), {});
  assert.deepEqual(await read(join(cwd, "missing")), {});
  assert.deepEqual(await read(tmpdir()), {});
  assert.deepEqual(await read("relative"), {});
  git("remote", "set-url", "origin", "git@github.com:team/app.git");
  git("checkout", "--orphan", "unborn");
  assert.deepEqual(await createGitMetadataReader()(cwd), {});
});

test("session producer uses actual pane cwd and keeps native revision independent", async t => {
  const { cwd, sha } = repository(t);
  const binary = join(cwd, "herdr.mjs");
  writeFileSync(binary, `#!/usr/bin/env node\nconsole.log(JSON.stringify({result:{pane:{cwd:${JSON.stringify(cwd)},revision:17}}}));\n`);
  chmodSync(binary, 0o755);
  const result = await enrichSession({ HERDR_BIN: binary }, {
    entity_type: "session", entity_id: "herdr:local:eng:pane", payload: {
      pane_id: "pane", host_id: "local", herdr_session: "eng", cwd: "/untrusted/event/cwd",
    },
  });
  assert.equal(result.payload.repository_id, "github.com/team/app");
  assert.equal(result.payload.commit_sha, sha);
  assert.equal(result.payload.revision, 17);
  assert.equal(result.payload.cwd, cwd);
});

test("native socket registry establishes exact server namespace without pane guessing", async t => {
  const { cwd } = repository(t);
  const binary = join(cwd, "registry.mjs");
  const socket = join(cwd, "herdr.sock");
  writeFileSync(binary, `#!/usr/bin/env node\nconsole.log(JSON.stringify({sessions:[{name:'eng',running:true,socket_path:${JSON.stringify(socket)}},{name:'other',running:true,socket_path:'/other/herdr.sock'}]}));\n`);
  chmodSync(binary, 0o755);
  const base = { HERDR_BIN: binary, HERDR_SOCKET_PATH: socket, HERDR_PLUGIN_EVENT: "pane.agent_detected", HERDR_PANE_ID: "w1:p1", HERDR_WORKSPACE_ID: "w1" };
  const routed = await resolveSessionEnv(base);
  assert.equal(routed.HERDR_SESSION_NAME, "eng");
  const envelope = mapEvent(routed, { host: "bender" }).find(e => e.entity_type === "session");
  assert.equal(envelope.entity_id, "herdr:bender:eng:w1%3Ap1");
  assert.equal(envelope.payload.herdr_session, "eng");
  assert.equal((await resolveSessionEnv({ ...base, HERDR_SOCKET_PATH: "/unknown/herdr.sock" })).HERDR_SESSION_NAME, undefined);
  assert.equal((await resolveSessionEnv({ ...base, HERDR_SOCKET_PATH: undefined })).HERDR_SESSION_NAME, undefined);
  assert.equal((await resolveSessionEnv({ ...base, HERDR_SESSION_NAME: "explicit" })).HERDR_SESSION_NAME, "explicit");
});

test("ambiguous or stopped socket registry matches remain telemetry-only", async t => {
  const { cwd } = repository(t);
  const binary = join(cwd, "registry.mjs");
  const socket = join(cwd, "herdr.sock");
  const env = { HERDR_BIN: binary, HERDR_SOCKET_PATH: socket };
  for (const sessions of [
    [{ name: "a", running: true, socket_path: socket }, { name: "b", running: true, socket_path: socket }],
    [{ name: "a", running: true, socket_path: socket }, { name: "a", running: true, socket_path: "/other/socket" }],
    [{ name: "a", running: false, socket_path: socket }],
  ]) {
    writeFileSync(binary, `#!/usr/bin/env node\nconsole.log(${JSON.stringify(JSON.stringify({ sessions }))});\n`);
    chmodSync(binary, 0o755);
    assert.equal(await resolveSessionEnv(env), env);
  }
});
