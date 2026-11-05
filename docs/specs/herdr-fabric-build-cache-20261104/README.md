# Herdr Fabric Build Cache — implementation bundle
Version: 2026-11-04. Status: implementation specification, NOT deployed.

## Objective
Reduce physical NVMe writes and repeat Rust compilation across Herdr-managed hosts; provide optional, safely isolated Slurm execution. Herdr Engineering owns policy and the Herdr dashboard. Pi Engineering is a consumer. Futurama hosts the Herdr control plane, not a required compilation or object-store bottleneck.

## Start here
1. Read `prompts/ONE_SHOT.md` and `specs/00-audit.md`.
2. Verify actual checked-out repository contents and all current local changes BEFORE editing.
3. Execute micro-specs sequentially with a commit, tests, and `HANDOFF.md` per step.
4. Never enable shared caching, deletion, remote build, or Slurm scheduling on fleet machines without passing the corresponding validation and explicit operator opt-in.

## Non-goals
- Replacing sccache or implementing a new compiler cache protocol.
- Sharing a writable Cargo target directory between machines.
- Implementing an additional Herdr dashboard, control plane, database, or Grafana.
- Making Slurm mandatory for local development.
- Using GPU nodes for ordinary CPU compilation unless explicit spare-capacity policy allows it.
- Changing InferWeave or independent Hive CI infrastructure unnecessarily.

## Existing reference surfaces (audit, not promises about current branch)
- `herdr_engineering/build_coord.py`, `tests/test_build_coord.py`, `docs/build-gate.md`
- `deploy/pi-build-gate/build-gate.ts`
- `herdr_engineering/build_cleanup.py`, `herdr_engineering/build_maintenance.py`, `docs/build-cleanup.md`
- `herdr_engineering/control_plane/slurm_placement.py`, `tests/control_plane/test_placement.py`
- `herdr_engineering/config.py`, `config/herdr-engineering.example.yaml`
- `herdr_engineering/fleet.py`, `ansible/tasks/build-cleanup.yml`
- existing `/api/v1` API, SSE, dashboard web client, metrics/export
- existing woodpecker-slurm persistent cache documentation and integrations (audit only)

## Repo
https://github.com/berlinguyinca/herdr-engineering

## Execution constraints
- Work on the host/repository explicitly selected by the operator. Never deploy to a guessed host.
- Fetch the zip from the Mac Downloads directory to the selected remote execution host if needed, verify sha256, then extract into a unique `docs/specs/herdr-fabric-build-cache-20261104/` path. If collision exists, compare checksums and ask for an explicit replacement rather than overwriting.
- Each micro-spec must fit a small-model context budget. Avoid unrelated refactors. Preserve user changes.
- Every spec produces a machine-readable evidence record and concise handoff.
