# Isolated dev stack for P01+ (Postgres + MinIO + control plane with HCN collab)

Script: `scripts/dev/uw-stack.sh` on branch `dev/uw-stack` (worktree `~/IdeaProjects/herdr-engineering-uw-dev`, from origin/main `676b442`). No equivalent existed (`scripts/dev-env.sh` only creates a venv; `deploy/control-plane/deploy.sh` is the production deploy and must not be used for dev).

## Isolation
- Own docker containers `herdr-uw-pg` (postgres:16-alpine) and `herdr-uw-s3` (MinIO RELEASE.2025-09-07, same stand-in as production), labelled `herdr.uw-stack=1`; named volumes `herdr-uw-pg-data`, `herdr-uw-s3-data`. The production compose project `herdr-control-plane` is never referenced.
- Control plane = a plain host process (`python -m herdr_engineering.cli control-plane web`) from the worktree, `--system-graph`, collab enabled via `--collab-users-file` (static bearer token for user `uw-dev-user`; no Cognito, no network egress).
- All listeners on 127.0.0.1 and below the kernel ephemeral range (32768+; Docker failed to bind 36900/39000 once because outbound sockets held them): **pg 31433, s3 31900, s3-console 31901, web+collab 31080**. Prod uses 80/9482/29999/15433/9000; fabric dev range is 18000-28999. Override with `UW_PG_PORT UW_S3_PORT UW_S3_CONSOLE_PORT UW_WEB_PORT`.
- Docker on bender is the shared rootful daemon (not rootless); isolation is by name, label, port and volume. No GPU, no host networking, no privileged flags.
- State and throwaway credentials (random, 0600) live in `~/.local/state/herdr-uw-stack/` (fleet-config.yaml, collab-users.json, web.log, web.pid, migrate.log). Nothing secret is written to git.

## Commands
```bash
cd ~/IdeaProjects/herdr-engineering-uw-dev
scripts/dev/uw-stack.sh up          # pg + s3 + migrate (all deploy/control-plane/migrations) + web; waits for /api/v1/health
scripts/dev/uw-stack.sh status      # containers, web pid, health: source=postgres rustfs=s3 caps incl. collab_v1
eval "$(scripts/dev/uw-stack.sh env)"   # HERDR_E2E_PG_DSN, UW_CONTROL_PLANE_URL, UW_COLLAB_TOKEN, UW_S3_ENDPOINT
scripts/dev/uw-stack.sh down        # stop web, remove containers; volumes kept
scripts/dev/uw-stack.sh down --purge  # also delete the two volumes (data loss: dev data only)
```
Collab call: `curl -H "Authorization: Bearer $UW_COLLAB_TOKEN" $UW_CONTROL_PLANE_URL/api/v1/collab/me`.

## Running tests against it
Only `HERDR_E2E_PG_DSN` opts tests into live Postgres (the e2e tests create and use a random schema per test). Everything else in `tests/control_plane` is in-memory.
```bash
eval "$(scripts/dev/uw-stack.sh env)"
python3 -m pytest tests/control_plane -q -k "collab or artifact or graph" -p no:cacheprovider
```
`collab-sandbox` tests spawn bubblewrap helpers and are slow under load; expect minutes, not seconds. Workspace tests may need `-j 4` equivalents per memory note when parallel.

## Verified run (2026-10-28, origin/main 676b442)
`pytest tests/control_plane -k "collab or artifact or graph"` with HERDR_E2E_PG_DSN pointing at the stack: **1994 passed, 1 skipped, 1 xfailed, 3225 deselected in 983 s** (loaded host). Stack was then taken down (`down`): no herdr-uw containers or web process left; volumes `herdr-uw-s3-data` / `herdr-uw-pg-data` remain (dev data only).

## Traps
- A fleet config needs both `sections` and `hosts: {}`; the S3 root user must be >= 3 chars and password >= 8.
- `/api/v1/health` here reports `commit: dev`; health can take seconds on first call (migration/hydrate).
- Do not run `scripts/dev/uw-stack.sh` on a machine whose ports 31xxx are in use; it refuses with "port busy".
