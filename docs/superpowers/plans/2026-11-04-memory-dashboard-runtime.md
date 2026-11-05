# Memory Dashboard Runtime Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. Follow the existing authorized memory plan; this slice supplies the missing runtime, not another registry.

**Goal:** Run the canonical pinned memory CLI behind the existing private Memory Explorer, with an explicitly supplied private client profile and unchanged viewer authorization.

**Architecture:** Keep `/api/v1/memory`, its principal/bank checks, and Hindsight unchanged. Build a small pinned CLI base on the deploy host without exporting Git credentials into an image. Materialize only engineering credentials into the web container at deployment, independently of user cache/outbox state, using the existing deploy/rollback machinery.

**Tech Stack:** Existing Python CLI, Python stdlib, existing YAML/fleet loader, Docker/Compose and Bash.

**Spec:** `docs/specs/herdr-unified-hindsight-memory-20261102/spec/ARCHITECTURE.md`, `docs/memory-explorer.md`, `docs/operations/memory-hindsight.md`.

## Global Constraints

- The control plane never talks directly to Hindsight; all reads use `herdr-memory` contract v1.
- Personal/assistant/Rakazo banks remain inaccessible to engineering viewers.
- No new service, store, API, UI, public listener, Funnel, or production Tailscale change.
- No credentials in image contexts, argv, logs or receipts. Credentials use private files and runtime stdin only.
- Preserve viewer default-deny. An empty viewer map exports no client credentials and still returns 403.
- Disabled/private-only deployments retain their existing behavior.

## Review Focus

- Dirty or differently checked-out SDK sources: export the exact locked commit without touching their worktrees.
- Source config and key symlinks, nonregular files, wrong owners or public permissions: reject before emitting an archive.
- Personal profiles and overly broad credentials: exclude private-assistant domains and select only configured engineering scopes.
- Forward-deploy failure and first activation: restore the prior private profile alongside the prior image/config.
- Missing or unavailable memory service: expose truthful unavailable/degraded state without taking down unrelated private APIs.

### Task 1: Pin the CLI in the runtime image

**Files:** Create `deploy/control-plane/prepare-memory-client.sh`, `deploy/control-plane/Dockerfile.memory-client`; modify `deploy/fabric-stack/Dockerfile`; test `tests/test_memory_client_image.py`.

**Interface:** `prepare-memory-client.sh [--source PATH]` reads `lock/upstreams.yaml` (`dependencies.herdr-memory`), exports its full immutable revision, builds a minimal image, and prints only its image tag. No host installation or harness settings change.

- [ ] Write subprocess tests for immutable export, dirty-source preservation, invalid pins and symlink rejection; observe failure.
- [ ] Implement export/build using only SDK `src`, `pyproject.toml` and README. Preserve default Python base unless deploy explicitly selects the CLI base.
- [ ] Run targeted tests, diff checks and a real `herdr-memory --version` container smoke; commit this slice.

### Task 2: Prepare a private service profile

**Files:** Create `herdr_engineering/memory_client_profile.py`; test `tests/test_memory_client_profile.py`.

**Interfaces:** `prepare_profile(source: Path, policy: dict, destination: Path, *, runner=None) -> dict` writes a 0600 tar containing `config.json` and credential files under fixed relative paths. It returns counts/digest only. `install_profile(archive: Path, container: str, *, runner=None) -> None` transfers via stdin and creates root-owned 0700/0600 runtime files under `/run/herdr-memory-client`; cache lives separately under `/state/herdr-memory-explorer`.

- [ ] Write failing permission, symlink/FIFO, scope filtering, empty-viewer, unsupported personal-project, malformed-input and no-secret-output tests.
- [ ] Reuse the canonical CLI for source-config contract validation; do not introduce a second transport or memory schema. Keep only the default credential binding used by the existing adapter, with an explicit nonpersonal default project for client identity/status.
- [ ] Verify archive paths, file modes and ownership in a temporary Docker container; reject unsafe archive members. Commit tested profile preparation.

### Task 3: Integrate optional deployment and validate end-to-end

**Files:** Modify `deploy/control-plane/deploy.sh`, existing deployment tests, and `docs/operations/memory-hindsight.md`.

**Interface:** An explicit `--memory-client-config PATH` supplies the operator profile. Enabled memory selects the pinned CLI base. Forward and rollback restore the corresponding generated private profile; never copy a forward profile into a previous deployment implicitly.

- [ ] Add subprocess regressions for disabled defaults, missing profile with configured viewers, first activation, correct rollback profile and redacted failures; observe failures before integration.
- [ ] Reuse `backup_file`, persistent attempt snapshots and `compose_up` rather than introducing a second rollout coordinator. Credentials go only into the private web container.
- [ ] Demonstrate real backend search/status in an isolated container with an explicitly authorized canary viewer; verify denied scopes and no host/public listener.
- [ ] Obtain independent review, run required checks, and make any production viewer change a separate explicit operator decision. Preserve current 403 until then.
