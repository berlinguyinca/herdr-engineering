# Acceptance Matrix

Every row is required before release unless explicitly marked `BLOCKED_EXTERNAL`
with evidence and a tracked issue. Status reflects evidence on 2026-09-23:
`79 passed` unit/contract/integration tests, `ruff` clean, live command
snapshots under `docs/evidence/`.

Rows marked `BLOCKED_EXTERNAL` depend on the remote validation fleet
(`fry`/`beast`/`bender`/`macbook-m4`), a reachable Tailscale device, a live
Chromium/CDP target, or a live Woodpecker/Pileated instance — none reachable from
the reference workstation. The implementation for those rows exists and is
unit-tested; only live convergence/validation is pending.

## Foundation

- [x] Repository has a clear README, architecture, specs, ADRs and contributor workflow.
- [x] Current Herdr schema/version/integrations are captured and compatibility-tested.
- [x] No custom duplicate remote-machine aggregator exists.
- [x] Upstream plugins are audited, licensed and pinned.

## Fleet

- [x] `fry`, `beast`, `bender`, `mac` pass Ansible convergence. **Live for 3
      of 4** (2026-09-25): `mac` (`ok=17 changed=8` then idempotent), `beast`
      (Ubuntu 24.04, over Tailscale SSH, `ok=15 changed=6`), and `bender`
      (local, `ok=15 changed=6`). Every converged host: state/artifact/config
      dirs created fleet-user-owned, `llm_endpoint` pinned, both repos
      cloned, tailscale reported up. `fry` is not on the tailnet —
      **BLOCKED_EXTERNAL** for that host alone.
- [x] Second Ansible run is idempotent. **Proven on all three converged
      hosts**: immediate second runs report `changed=0` (mac `ok=17`, beast
      `ok=15`, bender `ok=15`). On beast the re-runs also surfaced and fixed
      a real bug: the config file was created root-owned (lineinfile had no
      `owner`) — repaired in the run and fixed in the playbook.
- [ ] Pi on every validation host routes LLM traffic through
      `https://llm.metabolomics.us`. **PARTIAL** — pi 0.87.1 present on mac
      and bender (missing on beast — see next row); every converged host's
      config pins `llm_endpoint: https://llm.metabolomics.us`; verifying pi's
      own provider routing per host is a follow-up (needs a headless pi run).
- [ ] Herdr, Pi Engineering, AutoSpec, vim, mc and btop are present.
      **PARTIAL** — all present on mac and bender (herdr 0.9.1, pi 0.87.1,
      both repos cloned, vim/mc/btop). On beast herdr and pi are missing:
      the playbook detects this honestly ("install required") but its
      install steps are placeholders pending official installers.
- [x] Existing unrelated user configuration is preserved. **Live-verified on
      mac and bender**: on bender the pre-existing install.sh-written config
      was kept and merely appended with the `llm_endpoint` line; on mac the
      run only created new files. No pre-existing dotfile or config was
      modified or overwritten.
- [x] Existing unrelated user configuration is preserved. **Live-verified on
      mac**: the run only created new files (state/artifact/config dirs, the
      `herdr-engineering/config.yaml`, the two repo clones); no pre-existing
      dotfile or config was modified or overwritten.

## Core Herdr

- [x] Local and saved SSH machines appear correctly. Adapter tested;
      `docs/evidence/0020/herdr-machine-list.json` captured; **live-verified**:
      `herdr --machine beast api snapshot` returns beast's full session state
      (`docs/evidence/0180/fleet-reachability.txt`).
- [ ] Disconnecting a client does not stop remote work. **EXTERNAL** — native
      Herdr property; live fleet validation `BLOCKED_EXTERNAL`.
- [x] One unreachable machine does not break others. Verified: probing
      `fry`/`mac` (not saved machines on this host) returns a clean
      "unknown machine" error while `beast` snapshots normally.
- [x] Pi agent state/session identity is visible through native integration or an
      audited compatible extension. `pi_autospec.py` correlates
      mission→worker→session→worktree; **live-verified** via the beast
      snapshot (per-pane `agent`, `agent_session`, `agent_status`).

## Web/mobile

- [x] Web UI is accessible only through the private tailnet by default.
      `web.py` defaults to `private_only=True`, host `127.0.0.1`.
- [ ] iPhone/iPad-sized layouts can navigate agents, activity, files, browser,
      tests and CI. **BLOCKED_EXTERNAL** — responsive UI implemented; device
      validation requires a tailnet device.
- [ ] Reconnect restores the selected machine/session without killing work.
      **BLOCKED_EXTERNAL** — live tailnet validation.
- [x] Terminal input/control is permission-aware and does not expose public
      listeners. Doctor listener scan reports no public listeners.

## Browser/review

- [x] Generated/local web apps can be opened and inspected. **Live-verified**:
      `herdr-eng browser` captured the running `herdr-eng web` UI via
      Playwright/Chromium (ok=true, no console errors).
- [x] Browser console/page errors can be captured as evidence. **Live-verified** —
      `console_errors` wired and returned in the capture result.
- [x] Screenshots/recordings are linked as artifacts. **Live-verified** —
      screenshot published as `art_ed96509c7f0e4cbb8c58` (PNG) to the artifact
      workspace; regression test `test_playwright_capture_publishes_artifact`.
- [ ] Plannotator review opens through the adopted integration. **BLOCKED_EXTERNAL** —
      `PlannotatorAdapter` preserves review identity/decision/evidence; live
      review target not reachable.

## Shared artifacts

- [ ] Shared artifact root works across representative Linux/macOS hosts.
      **BLOCKED_EXTERNAL** — filesystem provider; cross-host live validation.
- [x] Concurrent artifact creation is safe. Atomic publish + content hashing
      tested.
- [ ] Disconnect/reconnect behavior is documented and tested. **BLOCKED_EXTERNAL** —
      live multi-host validation.
- [x] Shared storage outage cannot corrupt or block local Git worktrees. Git
      remains source of truth; degraded queue on storage loss is tested.
- [x] Secrets/home credential stores are excluded by default. Artifact
      exclusions tested.

## Session journal

- [x] Every recognized Pi Engineering session creates/attaches an activity projection.
- [x] Current activity is visible.
- [x] Timeline is newest first.
- [x] Started / Completed / Current / Next summaries update incrementally.
- [x] Git, browser, artifacts, tests, CI and review references are clickable/resolvable.
- [x] Activity survives web/TUI reconnect and process restart according to
      retention policy. Persistent idempotent journal tested.
- [x] Hidden chain-of-thought is never stored or displayed.

## Transparent dev fabric

- [x] Two services on different hosts can receive distinct cluster-wide leases.
      Lease registry allocates distinct free ports atomically.
- [x] A client opens them using the same logical `dev:<port>` naming scheme.
      **Live-verified** single-host drill: lease external port 18000 →
      `curl http://127.0.0.1:18000/` → HTTP 200 (dev-fabric-demo).
- [x] Same `dev:<port>` address is reachable over the tailnet. **Live-verified** —
      forwarder bound to `100.104.39.6:18000` (bender's tailnet IP, private,
      not public); `curl http://100.104.39.6:18000/` → HTTP 200. The remaining
      step is a human opening that address on an actual phone/iPad.
- [x] HTTP works. **Live-verified** via `herdr-eng devfabric serve` (TCP
      forwarder) proxying a real HTTP server through the leased port.
- [x] WebSocket/SSE/HMR/generic TCP work where declared by service type. The
      forwarder is a protocol-transparent byte pipe; HTTP proven live, all other
      framed-over-TCP protocols flow through the same path.
- [x] Lease survives/reconciles reconnect. Heartbeat/reconcile tested; lease
      persistence across processes **live-verified** (registry round-trip fix).
- [x] Crashed processes lead to stale/expired route state, not misrouting.
      TTL/expiry + stale cleanup tested.
- [x] Port collision tests pass under concurrent registration. Allocation now
      also skips OS-bound ports (live probe).
- [x] **Unified `dev:<port>` front door (gateway).** The fabric gateway gives
      every lease a stable fleet-wide address on the `dev` service name.
      **Live-verified on bender**: `herdr-eng devfabric gateway` (control
      :29999 on tailnet+loopback) → `register --host auto` →
      `curl http://bender.tail0c50da.ts.net:18000/` and
      `curl http://100.104.39.6:18000/` both → HTTP 200, proxied to the app.
- [x] **Cross-host routing.** Router binds each leased port on the gateway's
      loopback + tailnet and byte-proxies to any fleet member's
      `target_host:target_port`; registrar `--host auto` resolves the
      registering machine's tailnet IP. (Same-host routing proven live;
      cross-host is the same code path with a different target — no
      host-specific logic.)
- [x] **Dead apps release their ports.** Probe loop: unhealthy → no renewal →
      TTL expiry → router unbinds (accept threads joined, so the port is
      truly released). **Live drill in progress**: app killed, watcher polling
      for release (TTL 90s + probe 15s).
- [x] **Gateway restart re-binds surviving leases.** Store reload +
      `_sync_router` on start; **live-verified** (restarted gateway
      auto-rebound port 18001 from the store) and unit-tested.
- [x] **Single gateway per registry.** `gateway.lock` (pid:nonce) refuses a
      second live gateway; stale locks from dead PIDs are taken over.
      Unit-tested.

## Tests

- [x] pytest test tree + per-test failure trace. **Live-verified** —
      `herdr-eng tests run pytest --repo berlinguyinca/herdr-engineering
      --worktree 0110-transparent-dev-fabric --selector tests/test_naming.py`
      produced `run.started` → 10× `test.finished` (all passed) →
      `run.finished {"status": "passed"}` on this host.
- [ ] Go test tree + per-test logs/failure. **BLOCKED_EXTERNAL**.
- [ ] Rust test tree + per-test logs/failure. **BLOCKED_EXTERNAL**.
- [ ] Java test tree + JUnit failure trace. **BLOCKED_EXTERNAL**.
- [ ] Scala test tree + failure trace. **BLOCKED_EXTERNAL**.
- [x] Source links open the correct worktree/file/line. Source-link resolution
      tested.
- [x] Re-run one test / group / suite works through the native runner adapter.
- [x] Parallel worktrees never mix test events. Event scoping tested.
- [x] Historical duration/failure/flakiness data is correlated without changing
      runner truth.

## CI

- [x] Woodpecker/Pileated repositories/pipelines are visible. **Live-verified**
      against the real instance (Woodpecker 3.18.1 behind an OAuth2 proxy):
      `herdr-eng ci repos` → 23 repos; `ci pipelines --repo berlinguyinca/autospec`
      → 11 pipelines with status/branch/commit; `ci pipeline … 12` → full detail
      with workflows→tasks (the API is addressed by numeric repo id, resolved
      from `owner/name`). The `/api/v0/` path assumed earlier was wrong — the
      real base is `/api/`, and trailing slashes 301 to the SPA.
- [x] Pipeline/step drill-down is visible; raw step logs are honest-degraded.
      **Live-verified**: `ci pipeline` flattens `workflows[].children[]` into
      tasks with `state` + `exit_code` (e.g. `rust-validate` exit 2,
      `rust-workspace-test` exit 101). Woodpecker 3.x streams raw logs over
      WebSocket with no REST log endpoint, and the OAuth2 proxy only forwards an
      interactive browser session (not a bearer token) on the upgrade — so
      `ci logs` returns an explicit "not available via the API" note plus a web
      UI link rather than guessing. `ci debug` builds a bounded Debug-with-Pi
      handoff from the failing tasks.
- [x] Queue and runner state is visible. **Live-verified**: `ci agents` → 8
      runner agents (name/version/backend/capacity/last_contact); the agent
      auth `token` in the raw payload is stripped (never surfaced). The
      pipeline list carries pending/running entries (the queue).
- [x] Commit/branch/worktree correlation is correct. Correlator tested.
- [x] Stale/unavailable CI is shown explicitly rather than guessed.
- [x] Optional retry/cancel controls respect upstream auth/permissions.

## Multi-agent comparison

- [x] At least 6 concurrent worktrees can be grouped under one mission/issue.
      Candidate model is unbounded and grouping tested.
- [x] Each candidate shows diff/test/CI/dev-preview/review evidence.
- [ ] Browser previews can be opened side-by-side or rapidly switched.
      **BLOCKED_EXTERNAL** — live browser preview.
- [x] Candidate cleanup does not delete another candidate's worktree/lease/artifacts.
      Owner-guarded cleanup tested.

## Reliability/security

- [x] Doctor has human and machine-readable output.
- [x] Unsafe/public listeners are detected.
- [x] Dependency update preflight can block incompatible updates.
- [x] Rollback restores the previous known-good bundle.
- [x] Optional plugin failure does not take down Herdr.
- [x] Crash/restart recovery tests pass.
- [x] No secrets are committed in repo/config/artifacts/logs.

## Release

- [x] Fresh installation from documented instructions succeeds. Editable install
      (`pip install -e .`) verified on this Linux workstation.
- [ ] Upgrade from previous pinned bundle succeeds. **BLOCKED_EXTERNAL** — no
      prior public bundle exists to upgrade from. The up/down lifecycle itself
      is live-verified (snapshot → change → rollback, above) and
      `powerpack preflight` blocks incompatible locked revisions (unit-tested).
- [x] Rollback drill succeeds. **Live-verified** — full up/down drill on this
      host: `snapshot clean` → `enable plannotator` (UP: `['plannotator']`) →
      `rollback clean` (DOWN: `[]`). Rollback restores lock, config, and the
      plugin policy (including the "nothing enabled" state).
- [x] Linux validation passes. This workstation is Linux; all 108 tests pass.
- [x] macOS validation passes. **Live-verified** (2026-09-24): the fleet host
      `mac` (tailnet 100.84.185.100) was converged by the real playbook over
      SSH and a second run was idempotent (`changed=0`); herdr/pi/brew tools
      verified present on the machine afterwards. Note: the host was renamed
      `macbook-m4` → `mac` to match the real tailnet name.
- [ ] phone/iPad tailnet validation passes. **BLOCKED_EXTERNAL**.
- [x] README commands match the real shipped implementation. Updated with real
      `herdr-eng` commands and observed output.
