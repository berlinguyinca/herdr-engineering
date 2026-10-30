# P00 smoke tests (read-only, run 2026-10-28 ~20:35 PDT from bender)

Authorization: user stated nobody uses production Herdr; GET only. No mutation, restart, deploy. No credentials were needed (the dashboard is `--allow-open` on the tailnet; the collab operator identity is implicit). Ids, emails and message content are redacted below; only status codes and JSON shapes are recorded.

## Deployment map (docker + systemd on bender)
| Component | Evidence | Runs commit |
|---|---|---|
| Dashboard/control API (`herdr-cp-web`, `100.104.39.6:80`) | `docker ps`; `/api/v1/health` -> `commit` | `676b4420f1a4` = origin/main HEAD of herdr-engineering at audit time (PR #358) |
| Worker (`herdr-cp-worker`), collab edge (`herdr-collab-public`) | `HERDR_ENGINEERING_COMMIT` container env | `676b4420f1a4` |
| Fabric gateway (`herdr-fabric-gateway`, control API `:29999`) | container env | `17d2b21b3149` (older, ancestor of main) |
| Postgres 16 (`herdr-cp-postgres`), object store (`herdr-cp-rustfs`) | images `postgres:16-alpine`, `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z` | n/a |
| Public collab edge | `https://localdev.mcp.metabolomics.us` (nginx -> collab-public) | same image |
| Pi sessions/mission feed, service metadata, collab spawn/sync | systemd --user units (`herdr-cp-sessions`, `herdr-eng-mission-feed`, `herdr-service-metadata`, `herdr-collab-*.timer`) | host venv, not an image |

DB migrations applied in production: latest `0021_artifact_reports.sql` (2026-10-29 01:37 UTC), `0020_project_intelligence.sql`. So the audit baseline (`fb964ed`) is stale: 36 commits merged since, including **#348/#349/#351 (artifact reports + renderers), #317 (memory-hindsight), #353/#357 (dashboard refinement)**, all deployed.

Note: `:29999` is the fabric gateway, not the control API. `GET :29999/api/v1/health` -> 404 `{"error":"not found"}`. Health and all dashboard APIs are on `http://100.104.39.6` (the `herdr-cp-web` bind).

## Results (GET; base `http://100.104.39.6` unless noted)
| Path | Status | Shape / finding |
|---|---|---|
| `/api/v1/health` | 200 | `status, events, missions(74), sessions(431), hosts(15), services, inspector{...}, source:"postgres", rustfs:"s3", version:"0.1.0", commit:"676b4420f1a4", performance{...}, capabilities[]`. Occasional 500s in its own route stats (63 of 1469, max 10 s): health is slow under load. |
| capabilities | | `mission_activity_v1, ci_v1, plannotator_reviews_v1, github_v1, assistant_v1`. **Absent: `collab_v1` (on this web process), `project_intelligence_v1`, `architecture_v1`.** |
| `/api/v1/collab/config` | 200 | public Cognito config (client id, hosted UI domain, public_url). Also 200 on `https://localdev.mcp.metabolomics.us`. |
| `https://localdev.mcp.metabolomics.us/api/v1/collab/rooms` | 401 | unauthenticated, correct. `/api/v1/health` there is 404 (edge exposes collab only). |
| `/api/v1/collab/me` | 200 | `{user_id,email,status,approver,admin}` (operator identity; the web proxies collab to the operator listener `:9482`). |
| `/api/v1/collab/rooms` | 200 | `{rooms:[{room_id,name,role,project_id}]}` - 2 rooms. |
| `rooms/{id}`, `/events?limit=3`, `/state?wait=0`, `/tasks`, `/workspaces`, `/missions`, `/flow`, `/profile`, `/agents` | 200 | state: `seq, events[43], tasks[7], flow, links[0], missions[0], agents[0], people[1], requests[0]`. Room tasks exist (7). |
| `rooms/{id}/members`, `/attention`, `/progress`, `/architecture-links`, `/analytics` | 404 | **not deployed / not routes** at that path (room attention is not exposed; see ROUTES corrections). |
| `/api/v1/collab/projects`, `/api/v1/projects` | 404 | no top-level projects list route. |
| `/api/v1/artifacts` | 200 | `{artifacts:[{artifact_id,created_at,created_by,original_filename,display_name,mime_type,artifact_type,source,size_bytes,...}]}` - 74 artifacts. **Mount is `/api/v1/artifacts`, not `/api/v1/fabric/artifacts`** (that 404s). |
| `/api/v1/attention` | 200 | `{items[38]{key,fingerprint,kind,severity,title,detail,href,actions,source,last_seen,count,...}, counts{open,acknowledged,snoozed,resolved,critical}}`. **Mount is `/api/v1/attention`** (`/fabric/attention` 404s). |
| `/api/v1/publications` | 200 | `{publications:[]}` (none). |
| `/api/v1/graph/nodes?node_type=X` | 200 | `{node_type,nodes[],edges[],truncated,truncated_reason}`. **Empty for room, workspace, mission, plan, task, agent, attempt, session, message, issue, artifact, commit, pr, ci, review.** The lineage graph is wired but holds no data in production (no producer; `--system-graph` / lineage emitters not feeding it). `/api/v1/graph/nodes` without node_type -> 400 `unknown node_type`. `/api/v1/collab/graph/*` -> 404 through the web proxy. |
| `/api/v1/assistants` | 200 | 10 assistants (smoke-test debris, e.g. a "PA Smoke" owned by `open-listener`). |
| `/api/v1/assistant-approvals`, `/assistant-connectors` | 200 | both empty lists. |
| `/api/v1/assistant-runs`, `/assistant-memory` | 404 | not routes at that path. |
| `/api/v1/artifact-reports`, `/artifact-jobs`, `/artifact-reviews` | 404 | per-id routes only (not list roots); report support is deployed via migration 0021 and image commit, list roots do not exist. |
| `/api/v1/architecture`, `/api/v1/intelligence` | 404 | flags off. |
| `/api/v1/ingest/sources` | 200 | `sources: []`. |

Screenshots: NOT taken (no Playwright in the host python; skipped as not cheap). Dashboard HTML is served from the same container.

## Claim verdicts
- (a) **TRUE.** `herdr_engineering/control_plane/rustfs.py` exists (boto3 `RustFSClient`); compose service `rustfs` (`container_name: herdr-cp-rustfs`) uses the MinIO image; compose comments say RustFS has no maintained image. Deployed: `/health` says `rustfs:"s3"` and the running container is MinIO RELEASE.2025-09-07.
- (b) **Postgres backend IS active, as a single JSON snapshot.** Deployed fleet config has `personal_assistant.enabled: true` and `persistence.dsn` set; database `herdr_pa` holds table `pa_store_snapshot` with 1 row (key/data jsonb, updated 2026-10-28 16:52 UTC). `PostgresStoreBackend.persist` upserts the whole `AssistantStore` snapshot on mutation; the 12 normalized `pa_*` tables from migration 0014 exist but the store does not write them (snapshot model, not relational). The main `herdr` DB also has `pa_store_snapshot` with 0 rows. Implication: P07 must not assume relational `pa_*` tables are the source of truth.
- (c) **FALSE.** pi-engineering `origin/main` (`6082fb9`) `ControlPlaneIngest.emit` merges only `scopeIdsFromPayload(payload)` (ids already in the payload) and only when an envelope store is configured. `session()` builds a payload without room/workspace/task ids, and `ControlPlaneIngest.ts` never reads the YAML `herdr.room_id`. YAML `room_id` flows only into the *lineage* emitter (`herdrConnection.ts` -> `lineageConfig.ts` `defaultScope`). Session events on `/api/v1/ingest` therefore carry no room id: P02 must add it.
- (d) **Unrelated modules, name clash only.** `herdr_engineering/artifacts.py` = spec-0080 local `FilesystemArtifactWorkspace`/`ArtifactRef` handoff workspace (used by `cli.py` artifact/handoff commands and `doctor.py`). `herdr_engineering/control_plane/artifacts.py` = Postgres+RustFS `ArtifactStore` registry (used by `api.py`, `cli.py` control-plane path). Neither imports the other. Keep both; do NOT merge. The MATRIX CONSOLIDATE tag is downgraded to "rename/document only".

## Reproduce
```bash
curl -s http://100.104.39.6/api/v1/health | python3 -m json.tool | head -40
docker ps --format '{{.Names}} {{.Image}}'; docker inspect herdr-cp-web --format '{{range .Config.Env}}{{println .}}{{end}}' | grep COMMIT
docker exec herdr-cp-postgres psql -U <pg_user> -d herdr_pa -Atc 'select count(*),max(updated_at) from pa_store_snapshot'   # read-only
```
