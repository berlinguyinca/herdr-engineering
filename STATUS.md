# Implementation Status

**Overall:** All 18 numbered specs (0010–0180) are implemented as a thin,
curated integration layer over native Herdr. Implementation is COMPLETE.
Live validation that requires the external fleet/browser/CI instances is
documented as `BLOCKED_EXTERNAL` with evidence in `docs/acceptance-matrix.md`.

**Test evidence (2026-09-24):** `91 passed` via `python -m pytest -q` (2.4s);
`ruff check herdr_engineering tests` → **All checks passed!**; no pytest
collection warnings. Real command + live-fleet snapshots are captured under
`docs/evidence/`. Release tag: `v0.1.0`.

| Phase | Status | Evidence / PR / commit |
|---|---|---|
| 0010 Repository bootstrap & reconciliation | COMPLETE | `IMPLEMENTATION_RECONCILIATION.md`, `herdr-eng name`, naming tests |
| 0020 Upstream audit & compatibility | COMPLETE | `HERDR_COMPATIBILITY.md`, `lock/upstreams.yaml`, `docs/evidence/0020/` |
| 0030 Native Herdr contracts | COMPLETE | `herdr_adapter.py`, `herdr-eng capabilities`, adapter tests |
| 0040 Fleet bootstrap & Ansible | COMPLETE (impl); `mac` live-converged + idempotent; `fry`/`beast`/`bender` pending operator go-ahead | `ansible/playbooks/site.yml`, linux+macos roles, `ansible/README.md` |
| 0050 Powerpack distribution & lifecycle | COMPLETE | `powerpack.py`, `herdr-eng powerpack`, doctor tests |
| 0060 Web/mobile control surface | COMPLETE (impl); live tailnet `BLOCKED_EXTERNAL` | `web.py`, `herdr-eng web`, web tests |
| 0070 Browser preview & Plannotator | COMPLETE (impl); live Chromium `BLOCKED_EXTERNAL` | `browser.py`, `herdr-eng browser`, `herdr-eng review` |
| 0080 Shared artifact workspace | COMPLETE | `artifacts.py`, `herdr-eng artifacts`, artifact tests |
| 0090 Session journal & timeline | COMPLETE | `journal.py`, `herdr-eng journal`, journal tests |
| 0100 Pi Engineering/AutoSpec integration | COMPLETE | `pi_autospec.py`, correlation tests |
| 0110 Transparent dev fabric | COMPLETE (impl + gateway); cross-host live `BLOCKED_EXTERNAL` | `devfabric.py`, `fabric.py` (gateway/router/probe), `herdr-eng devfabric`, fabric+lease tests |
| 0120 Unified test explorer | COMPLETE | `tests.py`, `herdr-eng tests`, adapter tests |
| 0130 Woodpecker/Pileated CI | COMPLETE (impl + live); raw step-log text `BLOCKED_EXTERNAL` (WebSocket-only) | `ci.py`, `herdr-eng ci {repos,agents,pipelines,pipeline,logs,debug}`, CI tests |
| 0140 Multi-agent worktree comparison | COMPLETE | `candidates.py`, `herdr-eng candidates`, candidate tests |
| 0150 Unified engineering UX | COMPLETE | `web.py` navigation surfaces |
| 0160 Attention/notifications/review | COMPLETE | `attention.py`, `herdr-eng attention`, attention tests |
| 0170 Observability/security/update/rollback | COMPLETE | `observability.py`, `doctor.py`, listener tests |
| 0180 Fleet validation/release/docs | COMPLETE (docs/repo/CI); live fleet `BLOCKED_EXTERNAL` | README, `.github/workflows/ci.yml`, `scripts/`, acceptance matrix |

## One-shot installer

`scripts/install.sh` assumes Herdr already exists and sets up the HerdR
Engineering layer on the machine it runs on — without installing, upgrading,
or restarting Herdr, and without touching Herdr's state. It is idempotent and
enrolls the local machine (detected host + OS) into the private fleet
inventory. Verified on this host (a fleet member): Herdr binary unmodified and
still running after install.

```bash
cd herdr-engineering && scripts/install.sh
```

Machine-local config now auto-discovered by the CLI (`~/.config/herdr-engineering/config.yaml`),
overridden by repo `config/herdr-engineering.yaml`, then `$HERDR_ENGINEERING_CONFIG`.

## What exists (verified, not assumed)

- **Automatic semantic workspace naming** — `herdr-eng name`. Provenance-tracked
  (`name_source`), collision-safe, preserves user renames, at-most-one
  refinement. Real outputs (2026-09-23):
  - prompt *"Implement transparent dev routing so agents can preview from any
    device"* → **`transparent-dev-routing-agents`** (auto_prompt)
  - issue #421 *"Add test tree with pytest and JVM support"* →
    **`421-test-tree-pytest-jvm`** (auto_issue)
  - spec 0110 *"transparent dev fabric"* → **`0110-transparent-dev-fabric`**
    (auto_spec)
  - *"Refactor authentication to support Cognito groups"* →
    **`authentication-cognito-groups`** (auto_prompt)
  - *"can you please work on the browser integration"* → **`browser-integration`**
    (auto_prompt)
- **Doctor** — `herdr-eng doctor --json` reports config, lock, herdr
  capabilities (`machines=True snapshot=True plugins=True worktrees=False`),
  public-listener scan, tailscale, artifact workspace.
- **Powerpack** — `herdr-eng powerpack status|enable|disable|snapshot|preflight|rollback`.
- **Web** — `herdr-eng web --port 8787` private-by-default mobile-first UI.
- **Contracts** — five typed dataclasses (ActivityEvent, ArtifactRef,
  DevServiceLease, TestEvent, CIEvent) in `herdr_engineering/contracts.py`.

## Live validation performed on the fleet (2026-09-24, host `bender`)

- **Cross-machine Herdr access**: `herdr --machine beast api snapshot` returned
  beast's full live session state (7 workspaces, per-pane pi agent sessions
  with `agent_status` working/idle, protocol 22). One unreachable machine
  (`fry`/`mac`, not saved here) errors cleanly without affecting `beast`.
- **Dev fabric end-to-end**: registered a lease (external port 18000), ran
  `herdr-eng devfabric serve`, and `curl http://127.0.0.1:18000/` returned
  HTTP 200 from a real HTTP server on port 18777. The forwarder is a
  protocol-transparent TCP pipe (HTTP/WebSocket/SSE/HMR/generic TCP).
- **Browser preview**: `herdr-eng browser http://127.0.0.1:8787` captured the
  running web UI via Playwright/Chromium; the PNG was published to the
  artifact workspace as `art_ed96509c7f0e4cbb8c58`; `console_errors` came
  back empty.
- **Dev fabric gateway (unified `dev:<port>` front door)**: `herdr-eng
  devfabric gateway` on bender (control :29999 on tailnet+loopback,
  router on loopback+tailnet). `register --host auto` for a local app
  allocated external port 18000 and bound it immediately; both
  `curl http://bender.tail0c50da.ts.net:18000/` and
  `curl http://100.104.39.6:18000/` returned the app's page (HTTP 200).
  A gateway restart auto-rebound a surviving lease from the store
  (port 18001) without any registrar action. Dead-target port release is
  probe/TTL-driven (unhealthy → no renewal → TTL expiry → unbind with
  accept-thread join).
- Evidence: `docs/evidence/0180/live-validation.txt`,
  `docs/architecture/dev-fabric-gateway.md`.

Bugs found and fixed by the live drills: (1) `herdr-eng browser` never wired
the artifact workspace, so screenshots were captured but not persisted;
(2) dev-fabric lease persistence was broken across processes (`to_dict()`
nests fields the loader didn't reconstruct); (3) port allocation ignored
OS-level bindings; (4) an unbind could leak one connection through an
in-flight `accept()` (fixed by joining accept threads); (5) the probe loop
consumed expiry via `active_leases()`' internal reconcile and never released
ports (fixed with router self-sync); (6) `gateway_url_from_config` ignored
the `HERDR_ENGINEERING_FABRIC_URL` env var, silently degrading registrar
commands to local mode. All have regression tests.

## External validation blockers (with evidence)

| Blocked item | Reason | Evidence |
|---|---|---|
| Ansible convergence of `fry`, `beast`, `bender` | `mac` is converged and idempotent (see below); the remaining Linux hosts each need an operator go-ahead (installs packages on live machines) | `docs/acceptance-matrix.md` (Fleet) |
| Raw Woodpecker step-log *text* via API | Woodpecker 3.x streams logs over WebSocket (no REST endpoint); the OAuth2 proxy only forwards an interactive browser session, not a bearer token, on the upgrade. `ci logs`/`ci debug` degrade honestly and link the web UI | `docs/acceptance-matrix.md` (CI) |
| Human confirmation of phone/iPad on the tailnet | the `dev` front door is proven reachable from the tailnet (HTTP 200 via MagicDNS + IP); opening it on an actual device is a human step | `docs/evidence/0180/live-validation.txt` |
| Cross-host fabric routing drill | same code path as the proven same-host drill (target_host is a parameter); needs an app on a second fleet machine | `docs/architecture/dev-fabric-gateway.md` |

Cleared during live validation (2026-09-24): Chromium/CDP browser preview
(captured live, artifact published), dev-fabric HTTP on loopback and on the
tailnet interface, cross-machine Herdr access (`herdr --machine beast`),
the unified `dev:<port>` gateway front door on bender (MagicDNS + tailnet IP,
immediate bind, restart re-bind), the powerpack up/down (rollback) drill, the
live pytest test-tree run, the **live Woodpecker CI view** (repos,
runner agents, pipelines, pipeline→task drill-down with failing tasks + exit
codes, and a bounded Debug-with-Pi handoff — see below), the **real Ansible
convergence of the macOS fleet host `mac`** (first run `ok=17 changed=8
failed=0`, immediate second run `changed=0` — idempotent; herdr 0.9.1,
pi 0.87.1, vim/mc/btop via brew, both repos cloned, LLM endpoint config
written), plus GitHub push + CI (public repo
`berlinguyinca/herdr-engineering`, CI green).

- **Live Woodpecker CI (real instance, Woodpecker 3.18.1)**: with an operator
  token, `herdr-eng ci repos` → 23 repos; `ci agents` → 8 runner agents
  (auth `token` stripped); `ci pipelines --repo berlinguyinca/autospec` → 11
  pipelines with status; `ci pipeline … 12` → workflows→tasks incl. failing
  `rust-validate` (exit 2) and `rust-workspace-test` (exit 101); `ci logs`
  returns an explicit "not available via the API (WebSocket-only)" note + web
  UI link; `ci debug … 12` builds a bounded Debug-with-Pi handoff. The
  provider was corrected to the real API: base `/api/` (not `/api/v0/`),
  numeric repo ids resolved from `owner/name`, and 3.x `workflows[].children[]`
  (tasks) instead of a `steps` array. Evidence:
  `docs/evidence/0180/live-validation.txt`.

All implementation that does not depend on those external resources is
complete, tested, and committed.
