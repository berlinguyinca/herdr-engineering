# T16 end-to-end acceptance suite

Seven scenarios from `ACCEPTANCE.md` of the Herdr Federated Engineering program, run against **real processes on
loopback**: the Herdr control plane (`herdr-eng control-plane web`), Pi modules and CLI (plan `--store`, lineage
emitter and outbox, steering receiver, forks with real temp git repos and a real bare remote), and the real
`autospec` binary (`review-quorum publish | request | verify-report`).

One command, from the Herdr checkout root:

```
scripts/run-acceptance.sh              # add --build to cargo-build the autospec binary, --regression for the old suites
```

It fails on any failure, on a suite that ran **zero** tests, on a suite that ran **fewer** tests than
`tests/e2e/manifest.yaml` expects, and on any skip that is not a strict xfail. Results go to
`tests/e2e/.run/results.json` (untracked). Per-scenario:
`scripts/run-acceptance.sh --only s2,s5`. Herdr side alone: `python3 -m pytest tests/e2e -q` (marker `e2e`).
Pi side alone, per file and **without** `--test-force-exit`:
`cd <pi checkout> && node --test --import ./test/support/isolatedState.ts test/e2e/<file>.e2e.test.ts`.

## Configuration: YAML only, no environment variables

* `tests/e2e/acceptance.yaml` (paths to the Pi and AutoSpec checkouts, the autospec binary, Postgres bin dirs,
  timeouts). Copy to the untracked `acceptance.local.yaml` to point at other checkouts. Pi's side has its own
  `test/e2e/acceptance.yaml`.
* Every server is started with a **generated** 0600 fleet-config YAML (`control_plane.api_keys`,
  `lineage_key_scopes`, `review_quorum`, `collab.{delegation_enabled,steering,contextual,insights}`, `pg_dsn`), the
  production configuration path, plus a generated collaboration users file. Keys and tokens come from `secrets` at run
  time; nothing secret-like is committed.
* The only environment a child process sees is `PATH` (so the autospec/Pi children can find the fake `gh` shim).

## Topology

Two "hosts" share **one** control plane: server-to-server federation is a separate, later, gated feature (spec
"Security"), exactly as two machines would join one HCN room. A host is an ingest key with its own `EnvelopeStore`
source, host id, global `engineering.yaml` and temp git repo. Two workspaces are two temp repos. The restart scenario
uses a throwaway Postgres cluster on a unix socket (`tests/e2e/pg.py`).

## Rules these tests follow

* **Positive controls.** Every refusal (403/404/401) is paired with the same caller succeeding in the allowed case,
  and `Plane.verify_features` proves that steering, comments, contextual chat/consultation, delegation/proposals and
  insights are really switched on, so a 403 cannot be "feature off".
* **No fake progress.** Refused events are checked against `/api/v1/ingest/sources` (`last_sequence`, `gaps`) and node
  state before and after.
* **Gaps are expressed, not papered over.** A feature that is not wired is a Herdr `xfail(strict=True)` that asserts the
  *desired* behaviour (it turns into a failure when someone fixes it, forcing the marker to be removed) or, on the Pi
  side, a test named `GAP: ...` that asserts the *current* behaviour. A test never fills a gap by calling emitter
  primitives itself.
* **Fakes are marked.** `fake_gh*/gh` and the fake Plannotator approver carry a `FAKE - test shim` header. Nothing calls
  GitHub, a paid model, or a remote host.

See `docs/operations/acceptance-evidence.md` for scenario -> test -> result -> gaps and the exact commands/outputs.
