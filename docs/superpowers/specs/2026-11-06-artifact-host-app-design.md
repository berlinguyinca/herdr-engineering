# Artifact host: a standalone app for member artifacts

Status: draft for approval. Supersedes the deployment shape (not the product behavior) of
`2026-11-05-public-artifacts-frontend-design.md`. Product behavior (feed, folders, claim-by-sign-in, light-only public
pages, pending intake for agent pushes, expiry/revoke greying) is unchanged.

## Why

Phase 1 shipped as extra routes and containers of the control-plane image: the public containers share the image, the
repo's config, and a file hand-off with the control-plane worker, and enabling members forces `control_plane.api_keys`
(which flips the whole control plane to keyed mode). The owner's requirement is a boundary, not a hostname: the public
artifact app must be its own app, in its own container, with its own data, so that a mistake in it can expose artifacts
but never the private dashboard. The dashboard integrates the library as a read-only client.

## Goals

1. `artifact-host`: its own package, image, container(s), database and blob store. No credential, route or import path
   into the control plane.
2. Public face on `artifacts.local.metabolomics.us` (share links, member page, member API, agent MCP tools), unchanged
   for users.
3. Private face: the dashboard `/library` offers the same feed, filters, search, folders, item pages and share lists
   over **all** members' artifacts, **read-only**, by calling the host's internal API.
4. Members can be enabled without `control_plane.api_keys`.
5. Existing member artifacts, versions, blobs, folders, shares and claims migrate with verified counts and hashes.

## Non-goals / anti-goals

- No dashboard write path: no approve, revoke, file, delete from the dashboard. (Owner actions stay in the member page.)
- No call from the host to the control plane, ever; no shared Postgres, RustFS bucket or mounted directory.
- No dark mode on public pages. No phase-2 learning work here (the host is built so phase 2 can live in it).
- CI-report and agent-report artifacts stay in the control plane; they are not member artifacts.

## Architecture

```
internet -> router -> cubert SNI -> bender collab-edge (nginx, TLS)
   artifacts.local : /s/ /api/member/ / /my/ /mcp/artifacts  ->  artifact-host-public  (public listener, :8090)
   mcp.local       : /mcp (rooms) -> collab-public (unchanged);  /mcp/artifacts -> artifact-host-public (one agent URL)
tailnet only:  herdr-cp-web (dashboard)  --GET, key-authenticated-->  artifact-host-internal (:8091, network `artifact-internal`)
artifact-host-db (own Postgres)   artifact-host-blobs (own volume, content-addressed)
```

- **One app, two listeners.** `artifact-host-public` and `artifact-host-internal` are two processes of one image with
  different roles: the public role cannot import or mount the internal routes; the internal role is never published to
  the edge. Both reach the DB; neither reaches anything else.
- **Networks.** `artifact-host` network (public role <-> db <-> blobs, no egress). `artifact-internal` network contains
  only the internal role and `herdr-cp-web`. The edge reaches only the public role. The host containers have no route to
  `herdr-cp-*`; the control plane initiates every dashboard call.
- **Dashboard key.** One read-only key held by cp-web (fleet config `control_plane.artifact_library.host_key`), sent as a
  bearer token. The internal role accepts GET only and one role (`dashboard-read`); any other method is 405. The host
  stores only its digest.
- **Auth.** Members sign in with Cognito directly in the host (group `local.dev`; own AEAD session and claim-cookie keys,
  labels unchanged). Agents use host-issued bearer tokens minted from the member page, bound to one member; pushes land
  `intake='pending'` as today.
- **Data ownership.** The host's Postgres owns folders, items, share claims, shares, events, annotations, artifacts,
  versions, blob index and ACL for member artifacts. Blobs live in a content-addressed volume it owns. The member web
  page reads the DB directly (the file-view/worker-intent indirection and the `collab-public`/`share-public` pair
  disappear for this feature).
- **Dashboard viewer.** The dashboard operator is `dashboard-read`: sees everything; `viewers` mapping in the dashboard
  config is no longer needed for the operator and remains only to label rows.

## Read API (internal listener)

`GET /v1/feed`, `/v1/folders`, `/v1/items/<id>`, `/v1/items/<id>/versions`, `/v1/shares`, `/v1/search`,
`/v1/content/<item>/<version>/<path>` with the same query parameters the dashboard `/library` already uses
(`herdr_engineering/control_plane/artifact_library.py`: `feed`, `list_folders`, `count_pending`, `library_content`).
The dashboard page renders the host's JSON (MessagePack on the wire per standards; JSON accepted for browsers) with its
existing templates; cp-web adds an `ArtifactHostClient` and drops `LibraryStore`'s direct SQL.

## Migration and cutover

1. Build and deploy the host dark (own DB empty, edge unchanged, members still disabled).
2. `artifact-host import` reads the control plane via a one-shot read-only DB role (run by the operator, not by the
   host): copies rows where `created_by LIKE 'member:%'` plus library tables, claims, shares and events, and copies
   blobs by hash. It writes a manifest (row counts per table, blob count and total bytes, sha256 of every blob) and
   the verify step recomputes it on the host side; mismatch aborts.
3. Switch edge upstreams for `artifacts.local` to the host; enable the member routes. Old rows stay in the control plane
   untouched until the owner confirms removal.
4. Rollback: point the upstream back; the host's data is additive and the control plane's copy is unchanged.
5. A later PR removes the dead control-plane code (member intents, worker ingestion, collab-public member routes,
   `LibraryStore` SQL, the `members.enabled` api_keys prerequisite).

## Testing

TDD; no mocks. Real Postgres (testcontainers) for the host; unit, integration, smoke, Playwright E2E
(`node --test tests/playwright/*.test.mjs`). Specific boundary tests: public role has no internal routes; internal role
refuses non-GET and wrong keys; compose network test proves the host containers cannot resolve or reach `herdr-cp-*`;
import/verify test with a deliberately corrupted blob fails closed; dashboard `/library` renders identical rows to the
public feed for the same fixture.

## My calls (change any)

- Own Postgres container (PostgreSQL by default), not SQLite; blobs on a plain volume, not RustFS.
- Agents keep one URL: `mcp.local` routes `/mcp/artifacts` to the host. Room tools stay on `collab-public`.
- Host is a new top-level package `herdr_artifact_host/` in this repo with its own Dockerfile and CI job, not a new repo.
- Import is a one-time tool, not a sync; the control-plane copy is kept until the owner approves deletion.

## Settled during review (my calls, change any)

- **Cutover has no write window.** Production holds 0 member artifacts and 0 library items (all 77 artifacts are
  API-key created CI/agent reports; 4 share rows). Members stay disabled until the edge switch, so nothing is written
  to the old path. The import is still idempotent and re-runnable, and share-token digests are carried so existing
  share URLs stay valid. Rollback after real member data exists loses data written to the host since cutover; the
  runbook says so and takes a host DB dump first.
- **Single machine.** Edge, dashboard and host all run on bender (cubert only forwards SNI), so the Docker-network
  boundary is real. The internal listener binds only to `artifact-internal`, never to a host port.
- **The host runs its own background loop** for what the worker did for this feature: intent-free direct writes,
  expiry/revoke greying sweeps, pending intake caps, group-membership refresh and removed-member grace revocation.
- **Untrusted HTML never renders in the dashboard origin.** The dashboard embeds `/v1/content/...` only in an
  `<iframe sandbox>` (no `allow-same-origin`, no scripts unless the item type requires them in a separate origin) from
  the host's internal listener, which sends `Content-Security-Policy: sandbox` and a different origin (own port) than
  the dashboard.
- **MCP surface.** One path, `/mcp/artifacts`, served by the host (the seven artifact tools plus member push). The
  rooms server on `collab-public` is untouched and registers none of them.
- **Read API redaction.** Share tokens and agent-token digests never leave the host: shares are returned with
  metadata and an id only. Dashboard reads write nothing (no view events, no claim, no counters).
- **Existing credentials.** No agent tokens or member sessions exist yet, so none migrate; new session and claim keys
  are generated. Cognito callbacks for `artifacts.local` (`/`, `/s/callback`, `/my/callback`) are already registered.

## Left to the builder

Dockerfile and image-build recipe (none located yet for the control-plane image; follow how `deploy.sh` builds it),
exact schema (start from migration `0051` and the member/share tables), module layout, wire format for the read API,
compose/network names, and moving the `scripts/sync_share_tokens.py` styling into the host.
