# Rollout, canary, rollback, security (Pi-Bolt) — NOT approved for production

Standard Pi is and stays the default. Nothing below is applied to any production host. Every step that changes a default or
enables a canary requires explicit human approval (PB-014). Implementation: pi-engineering `src/runtime/piRuntime.ts`,
`scripts/pi-session-auto-resume.ts`, `scripts/pi-runtime.ts` (branch `feat/pi-bolt-runtime-adapter`, commit 890fbd0).

## 1. Version pinning and supply chain
1. Pin a release tag (evaluated: `bolt-v1.0.0`, Pi 1.4.0 base). Never use the `curl | sh` installer or auto-update on a managed host.
2. Download the tarball for the host (`linux-x64`, `linux-x64-jit`, `linux-arm64`, `darwin-arm64`) plus `SHA256SUMS` and `SHA256SUMS.sig`.
3. Verify: `openssl pkeyutl -verify -pubin -inkey release.pub -rawin -in SHA256SUMS -sigfile SHA256SUMS.sig` and `sha256sum -c`.
   Obtain `release.pub` out of band and record its fingerprint; in this evaluation it was fetched from the same repo (trust on first use).
4. Unpack into a versioned directory, off `PATH`. Record the sha256 of the `pi` executable and run
   `node scripts/pi-runtime.ts verify <path-to-pi> <sha256> <version>`; this writes `.pi-runtime-verified.json` beside it.
   At launch only the stamp's size/mtime/inode fingerprint and version are checked (hashing 263 MB per start is not free);
   a replaced or edited binary is refused and standard Pi runs.
5. Bundled components to review before any approval: Bun, JavaScriptCore (LGPL-2.0), ICU. Pi-Bolt is MIT.

## 2. Configuration (YAML only, no env switches)
`~/.pi/agent/engineering.yaml` (global) and `<repo>/.pi/engineering.yaml` (workspace; overrides global key by key):
```yaml
pi_runtime:
  default: standard-pi            # keep
  roles: { reviewer: pi-bolt }    # example: one role
  missions: {}                    # <mission-id>: pi-bolt | pi-bolt-jit
  canary: { runtime: pi-bolt, percent: 0 }   # 0 = off; deterministic by pane id
  binaries:
    pi-bolt: { path: /abs/path/pi, version: "1.0.0" }
```
Precedence (most specific first): role > mission > canary > default. Worker identity comes from `PI_ENGINEERING_ROLE`,
`PI_ENGINEERING_MISSION_ID` and `HERDR_PANE_ID` set by the spawner. GAP: HerdrAgentRuntime does not export the first two yet, so
role/mission overrides only apply where an operator sets them; tracked in PB-004/PB-008.
`node scripts/pi-runtime.ts status [cwd]` shows what the launcher would choose.

## 3. Canary plan (after approval only)
1. Host: bender (Linux x64, AVX2, glibc). Not macOS Intel, not musl hosts; unsupported hosts fall back automatically.
2. Start with `roles: { reviewer: pi-bolt }` or `canary.percent: 5`, `pi-bolt` only (not JIT), one host, quiet hours.
3. Watch `~/.pi/agent/runtime-selection.jsonl` (one line per launch: requested, runtime, source, fallbackReason) and agent success/quality gates.
4. Exit criteria: zero corrupted sessions, zero missing audit events, no gate bypass, fallbacks explained, quality parity.
5. Widen only with a new approval. JIT build only for extensions that need it (`bun:ffi`).

## 4. Rollback
- Immediate: set `default: standard-pi`, remove `roles/missions/canary`, or delete `binaries.pi-bolt`. Takes effect on the next agent start;
  running agents finish on the runtime they started on.
- Hard: delete the `.pi-runtime-verified.json` stamp; every launch then falls back (reason: no verification stamp).
- Sessions are Pi's own JSONL files; both runtimes read/write the same format. Session restore after rollback has NOT been
  drilled with real sessions (open item for PB-014).
- No replay: if a started Pi-Bolt agent exits non-zero, its exit code is propagated and the launcher does not re-run it on standard Pi.
  Only a failure to start at all (nothing ran) falls through to standard Pi.

## 5. Security notes
- Credentials are never passed on the command line and never written to logs; the selection log holds runtime, source and reasons only.
- The child gets the launcher's environment plus `PI_RUNTIME_KIND` and `PI_RUNTIME_SOURCE` (for the Herdr dashboard); no new secrets.
- The compiled binary has network and filesystem access equal to Pi's. Review before approval: network egress of Pi-Bolt itself
  (update checks; `--offline` / `PI_OFFLINE=1` disable startup network operations in Pi), and `pi-bolt doctor` behavior.
- A hash stamp is not a signature: whoever can write the install directory can restamp. Keep it root- or operator-owned.
