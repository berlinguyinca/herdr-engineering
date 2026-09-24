# Changelog

All notable changes to HerdR Engineering are documented here. Dates are
YYYY-MM-DD. The format follows Keep a Changelog.

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
