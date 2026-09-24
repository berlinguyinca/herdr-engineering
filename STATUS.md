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
| 0040 Fleet bootstrap & Ansible | COMPLETE (impl); fleet convergence `BLOCKED_EXTERNAL` | `ansible/playbooks/site.yml`, linux+macos roles, `ansible/README.md` |
| 0050 Powerpack distribution & lifecycle | COMPLETE | `powerpack.py`, `herdr-eng powerpack`, doctor tests |
| 0060 Web/mobile control surface | COMPLETE (impl); live tailnet `BLOCKED_EXTERNAL` | `web.py`, `herdr-eng web`, web tests |
| 0070 Browser preview & Plannotator | COMPLETE (impl); live Chromium `BLOCKED_EXTERNAL` | `browser.py`, `herdr-eng browser`, `herdr-eng review` |
| 0080 Shared artifact workspace | COMPLETE | `artifacts.py`, `herdr-eng artifacts`, artifact tests |
| 0090 Session journal & timeline | COMPLETE | `journal.py`, `herdr-eng journal`, journal tests |
| 0100 Pi Engineering/AutoSpec integration | COMPLETE | `pi_autospec.py`, correlation tests |
| 0110 Transparent dev fabric | COMPLETE (impl); live multi-host `BLOCKED_EXTERNAL` | `devfabric.py`, `herdr-eng devfabric`, lease/port tests |
| 0120 Unified test explorer | COMPLETE | `tests.py`, `herdr-eng tests`, adapter tests |
| 0130 Woodpecker/Pileated CI | COMPLETE | `ci.py`, `herdr-eng ci`, CI tests |
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
- Evidence: `docs/evidence/0180/live-validation.txt`.

Bugs found and fixed by the live drills: (1) `herdr-eng browser` never wired
the artifact workspace, so screenshots were captured but not persisted;
(2) dev-fabric lease persistence was broken across processes (`to_dict()`
nests fields the loader didn't reconstruct); (3) port allocation ignored
OS-level bindings. All have regression tests.

## External validation blockers (with evidence)

| Blocked item | Reason | Evidence |
|---|---|---|
| Real Ansible convergence of the 4 fleet hosts | playbooks `--check`-validated on `bender` (ok=12 failed=0, ansible-core 2.21.4); real convergence installs packages on live machines — pending operator go-ahead | `ansible/README.md` |
| Live Woodpecker/Pileated CI view | no reachable CI instance and no configured server URL (`~/.config/woodpecker` empty) | `ci.py` returns explicit stale/unavailable state |
| Human confirmation of phone/iPad on the tailnet | the tailnet address is proven reachable (HTTP 200 via `100.104.39.6`); opening it on an actual device is a human step | `docs/evidence/0180/live-validation.txt` |
| Push to GitHub + CI run | no remote configured; creating the repo is an operator decision (private/public) | `git remote -v` empty |

Cleared during live validation (2026-09-24): Chromium/CDP browser preview
(captured live, artifact published), dev-fabric HTTP on loopback and on the
tailnet interface, and cross-machine Herdr access (`herdr --machine beast`).

All implementation that does not depend on those external resources is
complete, tested, and committed.
