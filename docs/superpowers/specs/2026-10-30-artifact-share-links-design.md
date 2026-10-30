# Artifact share links (private / lab / public) — design

Status: draft for approval, 2026-10-30. Target: working end to end within one day.

## Intent

Artifacts are private today and the only way to show one to another person is to mail a saved preview file.
The operator needs to hand specific artifacts to specific audiences, tomorrow:

* **Private** (the default, unchanged): only the owner.
* **Lab**: a signed-in member of the lab's Cognito pool — one document for three named people.
* **Public**: anyone holding the link — one artifact for an external person with no account.

## Goals

* One link per share, pinned to ONE immutable artifact version; later edits never change what a link shows.
* Lab links open only after Cognito sign-in AND only for the people the owner chose (named emails and/or a Cognito group).
* Every link is created as a request, becomes live only on an explicit approve, and can be revoked instantly; public links always expire (default 30 days, owner may choose 1–90); lab links have no expiry unless the owner sets one.
* Works for every artifact profile (document, web, report, diagram, visualization, presentation), by sharing the version's primary rendered HTML output.
* An audit trail of create / approve / open / revoke.
* CLI (`herdr-eng artifacts share ...`) and skill documentation; no new login system.

## Non-goals

* No public access to the artifact API, source, uploads, other versions, feedback, or downloads. A share shows ONE rendered page.
* No per-viewer editing/commenting through a link.
* No new identity provider and no API keys (the control plane stays open; the owner is the single operator).
* No new public hostname (needs a cubert SNI change outside this repo); links live under `/s/` on the existing public collaboration host.
* No management web UI in this slice (CLI only); no email sending.

## Anti-goals (failure even if it "works")

* Any path from the internet to the artifact registry, RustFS, or an unauthenticated control-plane route.
* A lab link that opens for someone not in the pool or not on its list; a revoked/expired link that still serves.
* A link whose content changes after approval.
* Leaking whether a token exists (every failure is the same 404).

## Constraints from the codebase (verified 2026-10-30)

* `collab-public` (the internet-facing container behind the cubert SNI router and the bender nginx edge on `localdev.mcp.metabolomics.us`) deliberately has no database, AWS or blob access, and its edge allow-list 404s everything else. Keep that container unchanged.
* Cognito auth today is a bearer access token verified by `CognitoAuthenticator` (RS256, issuer, `token_use=access`, client allow-list, `cognito:groups` re-checked from `members.json`, JWKS refreshed by `collab-sync.sh` every minute). `verify_id_token` exists for the verified email claim. There are no server sessions or cookies; the login flow is client-side against `us-west-2gjtcm0pcp.auth.us-west-2.amazoncognito.com`. The pool is `us-west-2_GjtcM0PCp`, shared with `lc-binbase-scheduler` and the collaboration edge.
* Principals come only from `X-API-Key`; the open control plane makes everyone `operator`. `share` exists in the permission vocabulary but is enforced nowhere.
* Latest migration is `0035`; next is `0036`. `publications` (0004/0009) is a tailnet exposure controller and is NOT reused.
* Report previews are bounded inert HTML (CSP `default-src 'none'`, no script) but link to authenticated asset URLs, so a share serves a SNAPSHOT, not the live route.

## The design

### Data (migration `0036_artifact_shares.sql`)

* `artifact_shares`: `share_id`, `artifact_id`, `version_id`, `audience` (`lab` | `public`), `token_hash` (SHA-256 of a 256-bit random token; the token is shown once), `allow_emails text[]`, `allow_groups text[]`, `state` (`pending` | `live` | `revoked`), `expires_at`, `created_by`, `created_at`, `approved_by`, `approved_at`, `revoked_at`, `title`.
* `artifact_share_snapshots`: `share_id` (PK/FK), `content_type`, `body bytea`, `sha256`, `size_bytes` (≤ 8 MiB). The bytes are copied at APPROVE time from the version's primary rendered output (report: `render_report_preview`; others: the stored `text/html` output), so the public path needs no blob store and cannot drift.
* `artifact_share_events`: append-only `share_id`, `at`, `kind` (`created` | `approved` | `opened` | `denied` | `revoked` | `expired`), `actor` (principal, or Cognito `sub`/email for lab opens, or `public`), `detail` jsonb with no tokens or bodies.
* A least-privilege Postgres role `share_reader`: SELECT on `artifact_shares` (without `created_by` secrets, none exist) and `artifact_share_snapshots`, INSERT on `artifact_share_events`, nothing else.

### Owner API (control-plane-web, tailnet side, requires the existing `share` permission via `report_permission`)

* `POST /api/v1/artifacts/{id}/shares` `{version_id, audience, allow_emails?, allow_groups?, expires_in_days?, title?}` → `pending` share; returns `share_id` (no token yet).
* `POST /api/v1/artifacts/{id}/shares/{sid}/approve` `{confirm: true}` → renders the snapshot, sets `live`, returns the token URL ONCE (`https://localdev.mcp.metabolomics.us/s/<token>`). Same-principal approval is allowed (single operator) but must be a separate call with `confirm`; the CLI prints exactly what will be exposed (title, audience, version, size, sha256, expiry) before asking.
* `GET .../shares` (no tokens), `POST .../shares/{sid}/revoke`, `GET .../shares/{sid}/events`.
* Rules: lab shares require at least one of `allow_emails`/`allow_groups`; groups must exist in the pool; public shares cannot carry an allow-list; the version must exist and be `rendered`/valid; a draft/unvalidated version is refused.

### Public serving (new container `herdr-share-public`)

Same image, new flag `share_only` (pattern of `collab_only`): a minimal listener, DB via `share_reader`, no RustFS, no AWS, no API routes. Routes:

* `GET /s/<token>` — public share: hash the token (constant-time compare via the unique index lookup on the hash), require `live` and unexpired, return the snapshot with `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox; frame-ancestors 'none'`, `nosniff`, `no-store`, `no-referrer`, `X-Robots-Tag: noindex`. Lab share hit anonymously → the sign-in page instead of content.
* `GET /s/<token>` for a lab share returns a small static viewer page (`/s/static/share.js` + CSS, allow-listed): PKCE authorization-code login against the Cognito hosted UI (`/s/callback` on the same host), token kept in `sessionStorage`, then `GET /s/<token>/content` with `Authorization: Bearer <access token>` and the id token in `X-Id-Token`.
* `GET /s/<token>/content` (lab) — verify access token with `CognitoAuthenticator`, verify the id token for the verified email, then allow if the email is in `allow_emails` OR a token group is in `allow_groups`; membership staleness fails closed exactly like the collaboration edge (`members.json` > 10 minutes → deny). Returns the snapshot; the viewer renders it in a sandboxed `iframe srcdoc`.
* Every failure — unknown/revoked/expired token, wrong user, stale membership, oversize — is the same 404 body; the reason goes only to `artifact_share_events` (`denied`) and a rate-limited log.
* Edge: `deploy/control-plane/collab-edge/nginx.conf` renderer gets an upstream for `herdr-share-public` and a `^~ /s/` location (own `limit_req` zone, GET only, no body); everything else keeps the current allow-list.
* The collab slice renders `share` settings (`control_plane.share: {enabled, max_snapshot_bytes, default_public_days}`); the Cognito pool/clients/domain are the existing `control_plane.collab` values. `collab-sync.sh` already provides `jwks.json`/`members.json`; the share container mounts them read-only.

### Owner experience

`herdr-eng artifacts share create|approve|list|events|revoke ...`, `--audience lab --email a@x --email b@x [--group artifacts-users]`, `--audience public --expires-in-days N`. `herdr-artifact` skill gets a share reference page; the safety contract text is updated (shares are pinned, revocable, approve-gated).

### Deploy order

1. Migration + `share_reader` role (idempotent; role password from fleet config YAML, never env/GitHub).
2. Control plane web/worker (owner API) — usable immediately for private/owner flows.
3. `herdr-share-public` container + edge regeneration (nginx reload) via `deploy.sh`.
4. Cognito web-client change (below), then lab smoke test with the owner's own account.

## Decided by the operator

Three audiences (private default, lab = the `lc-binbase-scheduler` pool, public); one document to three lab people and one public artifact to an external user tomorrow; the full feature, not a first slice; no API keys; reuse the existing pool and edge.

## My calls (change any)

* Links live under `/s/` on the existing public host rather than a new hostname.
* Snapshot-at-approve instead of reading live versions.
* New `artifact_share*` tables instead of reusing `publications`.
* A separate `herdr-share-public` container instead of widening `collab-public`.
* Public default expiry 30 days (required, 1–90); lab links optional expiry, none by default.
* Single-operator approval via a separate confirmed call.
* Lab authorization: email in the list OR group in the list.
* Max snapshot 8 MiB; HTML only (a PDF/other output is not shared in this slice).

## Needs the operator (outside the code)

* Registering `https://localdev.mcp.metabolomics.us/s/callback` (and logout) as a callback URL on the Cognito web app client `cognito_web_client` is a change to a shared production pool client — it needs the operator's go-ahead before it is made.

## Builder-check resolutions (2026-10-30)

* **Cognito client** (verified read-only): `cognito_web_client` is `herdr-collab-web`, dedicated to the collaboration page (not shared with lc-binbase-scheduler), code flow, no client secret, scopes `openid email`, callbacks today `https://bender.tail0c50da.ts.net:9447/` and `https://localdev.mcp.metabolomics.us/`. The only AWS change is ADDING `https://localdev.mcp.metabolomics.us/s/callback` to its callback and logout URLs; it is made at deploy time with the operator's go-ahead. The build confirms the client is on the authenticator's client allow-list and adds it to the share slice if not; if the client cannot serve the flow, stop and report instead of creating a new login system. The lab path is also tested against a fake JWKS/Cognito (pattern of `test_collab_cognito.py`).
* **Named emails vs membership:** an email in `allow_emails` is authorised by the VERIFIED email claim of a valid id token from the pool (token_use=id, client allow-listed), without needing a group or `members.json`; `allow_groups` uses `cognito:groups` plus the fresh-membership rule (stale `members.json` > 10 min denies). The owner must confirm each named person has a pool account (`aws cognito-idp admin-get-user`) before approving; the CLI does this check when AWS credentials exist and warns otherwise.
* **Which artifacts:** the version's primary output is the first `text/html` output in its manifest order (report: the rendered preview). A version with no HTML output cannot be shared (refused at create). Script, external assets and linked images are not served: at approve time `img` and `url()` references to the artifact's own assets are inlined as data URIs when the total stays under the 8 MiB cap, anything else is stripped and reported in the approve summary. The snapshot is served with `script-src 'none'`, so presentation/web artifacts that need JavaScript show their static HTML only; the approve summary says so.
* **Scope:** this plan includes the deploy and a live smoke test (private, public, lab with the owner's own account). Every production step — migration, `share_reader` role, rollout, Cognito callback — happens only with the operator's go-ahead at that moment.
* **Share container and Postgres:** `herdr-share-public` joins only the compose network that reaches Postgres directly (same path as control-plane-web, no PgBouncer), with a `share_reader` login limited to SELECT on `artifact_shares`/`artifact_share_snapshots` and INSERT on `artifact_share_events`. It cannot read artifacts, versions, blobs or any other table, and the snapshots it can read are content the owner already approved for exposure. This is a deliberate, minimal widening of the boundary; the alternative (an internal HTTP hop through control-plane-web) would put a second internet-facing path to the full API and is rejected.

## Left to the builder

Module layout, exact SQL, the viewer JS, nginx snippet details, test structure, wording of CLI output, the `X-Robots-Tag`/rate-limit numbers, how `share_only` is wired into compose.
