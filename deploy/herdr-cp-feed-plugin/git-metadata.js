// Local checkout evidence for exact CI links. Never contact a remote forge.
import { execFile } from "node:child_process";
import { isAbsolute, normalize } from "node:path";

export function canonicalRepository(value) {
  if (typeof value !== "string" || value.length > 2048) return null;
  value = value.trim();
  if (!value || value.startsWith("/") || /[\x00-\x1f]/.test(value)) return null;
  const scp = value.match(/^[^/@:]+@([^/:]+):(.+)$/);
  if (scp) value = `ssh://${scp[1]}/${scp[2]}`;
  if (!value.includes("://")) {
    if (!value.split("/", 1)[0].includes(".") || !value.includes("/")) return null;
    value = `https://${value}`;
  }
  try {
    // Inspect the original path before URL normalizes dot segments away.
    const rawPath = value.match(/^[^:]+:\/\/[^/]*(\/[^?#]*)?/)?.[1] || "";
    let path = decodeURIComponent(rawPath).replace(/^\/+|\/+$/g, "");
    if (path.endsWith(".git")) path = path.slice(0, -4);
    const parts = path.split("/");
    if (parts.length < 2 || parts.some(p => !p || p === "." || p === "..") || /[\s\x00-\x1f?#\\]/.test(path)) return null;
    const parsed = new URL(value);
    const defaults = { "https:": "443", "http:": "80", "ssh:": "22", "git:": "9418" };
    if (!(parsed.protocol in defaults) || !parsed.hostname) return null;
    let host = parsed.hostname.toLowerCase().replace(/\.$/, "");
    if (parsed.port && parsed.port !== defaults[parsed.protocol]) host += `:${parsed.port}`;
    if (host === "github.com") path = path.toLowerCase();
    return `${host}/${path}`;
  } catch { return null; }
}

function readGit(cwd, args) {
  // Inherited GIT_DIR/GIT_WORK_TREE must not redirect evidence to another repo.
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith("GIT_")));
  env.GIT_TERMINAL_PROMPT = "0";
  return new Promise(resolve => {
    execFile("git", ["-C", cwd, ...args], { timeout: 1500, maxBuffer: 4096, env },
      (error, stdout) => resolve(error ? null : String(stdout || "").trim()));
  });
}

export function createGitMetadataReader({ now = Date.now, ttlMs = 30000, maxEntries = 128 } = {}) {
  const cache = new Map();
  return async function gitMetadata(cwd) {
    if (typeof cwd !== "string" || cwd.length > 4096 || !isAbsolute(cwd) || /[\x00-\x1f]/.test(cwd)) return {};
    cwd = normalize(cwd);
    const previous = cache.get(cwd);
    if (previous && now() - previous.at < ttlMs) return { ...await previous.value };
    const value = Promise.all([
      readGit(cwd, ["remote", "get-url", "origin"]),
      readGit(cwd, ["rev-parse", "--verify", "HEAD"]),
    ]).then(([remote, commit]) => {
      const repository = canonicalRepository(remote);
      // Emit the pair together: a SHA without a known origin is not a CI link.
      return repository && /^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$/.test(commit || "")
        ? { repository_id: repository, commit_sha: commit.toLowerCase() } : {};
    });
    cache.delete(cwd);
    cache.set(cwd, { at: now(), value });
    while (cache.size > Math.max(1, maxEntries)) cache.delete(cache.keys().next().value);
    return { ...await value };
  };
}

export const gitMetadata = createGitMetadataReader();
