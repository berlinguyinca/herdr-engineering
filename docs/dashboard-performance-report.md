# Dashboard Performance Investigation & Fixes

Branch: `fix/health-lean-probe` (based on `origin/main`). Scope: control-plane
`/api/v1/health` latency + dashboard data-fetch weight. Read-only investigation
findings are marked **[measured]** / **[observed in production]** / **[analysis]**.

## 1. Root cause of the deploy false-negative — **[observed in production]**

`/api/v1/health` returns a heavy payload. Under load on `badc3f7afc05` it took
**3–13 s** and intermittently returned **HTTP 500 (BrokenPipeError)**. `deploy.sh`'s
`healthy_commit()` gate polls `curl -fsS -m 5 "$HEALTH_URL"`; a >5 s health makes the
curl time out, so both the deploy gate and the auto-rollback path reported a *failure*
while the container was actually healthy — a false negative.

Why the payload is heavy — **[analysis]** of `_health()` (`api.py`):
- `self.repo.health()` runs the inspector's per-session scan (sums events/bytes across
  every session) — cost scales with **total events**, not sessions.
- `self.request_metrics.snapshot()` embeds `by_route` (per-route aggregates),
  `by_path`, and a `recent_requests` ring — all grow with traffic.
- Plus repo counters (`events`, `missions`, `sessions`, `hosts`, `services`).

The existing lean path only fired when `_role() is None` (keyed mode, no valid key).
In **keyless/open** deployments `_role()` returns `"operator"`, so health fell through
to the full heavy path — the production posture.

## 2. Fix — O(1) `?probe=1` shape — **[measured]**

Added `ControlPlaneServer._health_probe()` returning exactly
`{status, version, commit, capabilities}`. It reads **only** in-memory config:
no `repo.health()`, no inspector scan, no metrics snapshot. It preserves the
`status` + `commit` fields `healthy_commit()` parses, so the deploy gate is unchanged.

Measured on a seeded in-memory server (40 sessions, 1000 recorded events):

| shape | bytes | top-level keys | build cost |
|-------|------:|----------------|------------|
| full `/api/v1/health` | 879 | 13 (incl. `inspector`, `performance`, `events`, …) | grows with events + routes |
| `?probe=1` | 142 | 4 (`status, version, commit, capabilities`) | O(1), load-independent |

**83.8 % payload reduction at small scale, and the probe is fixed-size / O(1)
regardless of load**, whereas full health grows with `by_route` + `recent_requests` +
`inspector` per-session data. (The tiny in-memory fixture does not itself reproduce the
production 3–13 s; that figure is the empirical production observation that motivated
the fix.)

Wired to all three hot spots that call health on a critical path:
- `deploy/control-plane/deploy.sh` → `HEALTH_URL=…/api/v1/health?probe=1` (deploy gate).
- `packages/app.js` → `client.health({ probe: true })` (nav-capability probe on first paint).
- `packages/overview-view.js` → `client.health({ probe: true, signal })` (landing-page
  5 s refresh; it reads only `health.status`).

Default `/api/v1/health` contract is **unchanged** (still carries `inspector` +
`performance` + counters) — no consumer or assertion weakened.

## 3. Polling & fetch patterns — **[analysis]** (no change needed)

- The `setInterval(…, 1000)` calls in overview/topology/github/mission-catalog/
  session-workspace are **clock redraws** (`draw`/`tick` re-render already-loaded data),
  not server polls — cheap DOM work.
- Real data refresh uses `createResourcePoller` (`refresh-bus.js`): `intervalMs = 5000`,
  `concurrency = 4`, visibility-aware (aborts in-flight requests when `document.hidden`),
  exponential backoff on failure. This is sound; no change made.
- **No genuine N+1 fetch loops found.** The static scan flagged ~17 "possible N+1" sites
  in `personal-assistant-view.js`; manual inspection showed they are sequential `await`s
  inside click/event handlers (one action → re-render), not per-item fetch loops.

## 4. Verification

- `tests/control_plane/test_health_probe.py` — 4 new tests: lean shape, keyless/open
  mode, deploy-gate fields present, default contract preserved.
- Existing health-contract tests still green: `test_s4_external.py` (pins keyed-no-key
  shape to `{status, version, commit}`; keyed answer richer), `test_inspector.py:470`
  (default health keeps `inspector.events`), `test_central_config_servers.py`,
  `test_deploy_scripts.py` (stub glob `*api/v1/health*` still matches `?probe=1`).
- Full JS suite: 354 pass. Python health suite: 235 pass.

## 5. Not done (needs browser + operator sign-off)

**Visual polish pass** (layout/spacing/responsiveness at 1280 / 375 px with screenshot
evidence) is **not** included. It requires Playwright + the pinned Chromium build
(absent here; visual baselines are Chromium-build-pinned and "would fail wholesale on
another machine"), subjective design iteration across surfaces × viewports × themes, and
baseline regeneration — all gated on operator visual approval.
