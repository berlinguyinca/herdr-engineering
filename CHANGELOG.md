# Changelog

All notable changes to HerdR Engineering are documented here. Dates are
YYYY-MM-DD. The format follows Keep a Changelog.

## [Unreleased]

### Added
- **Dev fabric gateway** — the unified `dev:<port>` front door (spec 0110,
  component #3). `herdr-eng devfabric gateway` runs the fleet gateway:
  control API (register/heartbeat/close/list/resolve/healthz) on
  tailnet+loopback with optional bearer token, a dynamic router that binds
  each leased external port on loopback + tailnet and byte-proxies to the
  owning machine, and a probe loop that renews healthy leases, expires dead
  ones (TTL), and releases their ports. Every lease gets a stable fleet-wide
  address `http://<gateway-magicdns>:<port>` (e.g.
  `http://bender.tail0c50da.ts.net:18000`) reachable from any tailnet device.
- `HERDR_ENGINEERING_FABRIC_URL` / `fabric.gateway_url` config: registrar
  commands route through the gateway automatically and degrade to local mode
  with an explicit warning when it is unreachable.
- `--host auto` for `devfabric register`: resolves the local tailnet IPv4 and
  verifies the target is listening before publishing the lease.
- `gateway.lock` (pid:nonce): refuses a second live gateway over one store;
  stale locks from dead PIDs are taken over.
- Fabric gateway tests (register/route, close/unbind, dead-target expiry,
  healthy renewal, token auth, list/resolve, restart re-bind, port-taken
  refusal, URL resolution, single-gateway lock, API shape).
- `docs/architecture/dev-fabric-gateway.md` (design + live evidence).
- **Live Woodpecker CI view** — validated against the real instance
  (Woodpecker 3.18.1). `herdr-eng ci repos|agents|pipelines|pipeline|logs|
  debug`: repos, runner agents (auth token stripped), pipelines with status,
  pipeline→task drill-down (3.x `workflows[].children[]` flattened to tasks
  with state + exit code), honest step-log handling, and a bounded
  Debug-with-Pi handoff for failing pipelines.
- **Real Ansible convergence of three of four fleet hosts** — `mac` (macOS,
  over the tailnet), `beast` (Ubuntu 24.04, over Tailscale SSH) and `bender`
  (local): each converged then immediately idempotent (`changed=0`).
  Inventory renamed `macbook-m4` → `mac` to match the real tailnet name;
  per-host connection details moved to gitignored `inventory/host_vars/`.
  `fry` is not on the tailnet (blocked for that host alone).
- Ansible fixes found during the live runs: brew check gated on a
  `brew_present` fact (the old check always exited 0, so brew tasks would
  fail on a Mac), `become` overridable (`-e herdr_engineering_become=false`)
  for user-scoped hosts without passwordless sudo, play PATH now includes
  the Homebrew prefixes and the fleet user's `~/.local/bin` (non-interactive
  SSH on macOS does not source the login PATH — and the setup-task
  interaction made herdr checks fail on every run), the tailscale report
  no longer matches a word that `tailscale status` never prints, and the
  config `lineinfile` now sets `owner` — with `become: true` it created the
  fleet user's config root-owned and unreadable by that user (caught by the
  idempotency re-run on beast, which also repaired the live file).

### Fixed
- **CI provider corrected to the real Woodpecker 3.x API** (found during live
  validation): base is `/api/`, not `/api/v0/` (the earlier `/v0` was a
  mis-diagnosis caused by trailing-slash 301s returning the SPA); repos are
  addressed by numeric id (resolved from `owner/name`); pipeline detail uses
  `workflows[].children[]` (tasks), not a `steps` array; offline `pipelines`/
  `pipeline`/`step_log` return empty/None instead of raising.
- `ci logs` no longer implies logs are missing due to a bug: it reports
  explicitly that this Woodpecker 3.x build streams logs over WebSocket (no
  REST endpoint) and the OAuth2 proxy does not forward a bearer token on the
  upgrade, then links the web UI.
- Fabric router `unbind` leaked one connection through an in-flight
  `accept()`; unbind now joins the accept threads so a port is only reported
  released when it truly is.
- The probe loop never released ports for expired leases (expiry was
  consumed by `active_leases()`' internal reconcile); the router now
  self-syncs to the active set each cycle (self-healing after restarts and
  closes).
- `gateway_url_from_config` ignored the `HERDR_ENGINEERING_FABRIC_URL`
  environment variable, silently degrading registrar commands to local mode.
- Registry store writes from an overlapping process could be lost on the
  next persist; the gateway now reloads the store when its mtime changes.
- Test port ranges were host-dependent; allocation tests now pick
  OS-free ports so they are hermetic.

## [0.1.0] - 2026-09-24

First public-quality release of the HerdR Engineering integration layer.
Implements all 18 dependency-gated specs (0010–0180) plus the five typed
contracts, as a thin curated layer over native Herdr 0.9.1 (verified).

### Added
- `herdr-eng` CLI + `herdr_engineering` Python package implementing all 18
  numbered specs: naming, Herdr adapter, powerpack lifecycle, web/mobile
  surface, browser preview + Plannotator, shared artifacts, session journal,
  Pi/AutoSpec correlation, dev fabric, test explorer, CI adapter, candidate
  comparison, attention, observability/security, doctor.
- Automatic semantic workspace naming (`deriveWorkspaceIdentity`) with
  `name_source` provenance, collision safety, user-rename preservation.
- One-shot installer `scripts/install.sh` (assumes Herdr exists; idempotent;
  enrolls the local machine into the private fleet inventory and, when a key
  is provided, onto the Tailscale tailnet; never modifies Herdr or its state).
- Config auto-discovery with deep-merge precedence:
  machine-local < repository < `$HERDR_ENGINEERING_CONFIG`.
- Ansible fleet playbooks (Linux + macOS) and GitHub Actions CI.
- 91 unit/contract/integration tests (ruff-clean, no collection warnings).
- Five typed contracts (ActivityEvent, ArtifactRef, DevServiceLease, TestEvent,
  CIEvent) as validated dataclasses.

### Fixed (found by live validation drills)
- `herdr-eng browser` now wires the artifact workspace so screenshots are
  actually persisted as artifacts (spec 0070).
- Dev-fabric lease persistence now round-trips across processes (the loader
  reconstructed nested `target`/`owner`/`health` fields).
- Dev-fabric port allocation now skips OS-bound ports (not just other leases).
- `test_build_tree` no longer recurses pytest over the whole repo (was 300s).
- pytest adapter captures passing tests (`-rA`) and strips trailing messages.

### Security
- Private by default: web binds `127.0.0.1`; dev-fabric refuses public bind
  addresses; no secrets in repo/config/artifacts/logs; hidden chain-of-thought
  never persisted.

### Validation
- `91 passed`, `ruff` clean.
- Live: cross-machine Herdr snapshot via `herdr --machine beast`; dev-fabric
  HTTP drill (leased port 18000 → HTTP 200); browser capture published as an
  artifact. See `docs/evidence/0180/live-validation.txt`.
- Remaining `BLOCKED_EXTERNAL` items (live Woodpecker CI, iPhone/iPad tailnet,
  full Ansible convergence) are documented in `docs/acceptance-matrix.md`.

[0.1.0]: https://github.com/berlinguyinca/herdr-engineering/releases/tag/v0.1.0
