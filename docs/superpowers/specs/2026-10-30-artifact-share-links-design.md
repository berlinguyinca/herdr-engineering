# Artifact share links (private / lab / public) — design

Status: revision 2 for approval, 2026-10-30. Target: working end to end within one day.
(Revision 1 shared one frozen HTML page. The real artifacts are PDFs, HTML, and HTML + parquet collections that load
data dynamically, so a share is now a frozen FILE BUNDLE served with range requests and scripts allowed to run.)

## Intent

Artifacts are private today and the only way to show one to another person is to mail a saved file.
The operator needs to hand specific artifacts to specific audiences, tomorrow:

* **Private** (the default, unchanged): only the owner.
* **Lab**: a signed-in member of the lab's Cognito pool — one document for three named people.
* **Public**: anyone holding the link — one artifact for an external person with no account.

## Goals

* One link per share, pinned to ONE immutable artifact version; later edits never change what a link shows.
* A share can be: a PDF; an HTML page; or an HTML page plus the files it loads at run time (parquet, JSON, CSV, JS, CSS, images, wasm), with relative URLs and HTTP range requests working.
* Lab links open only after Cognito sign-in AND only for the people the owner chose (named emails and/or Cognito groups).
* Every link is created as a request, becomes live only on an explicit approve, and can be revoked instantly. Public links always expire (default 30 days, 1–90); lab links have no expiry unless the owner sets one.
* An audit trail of create / approve / open / denied / revoke.
* CLI (`herdr-eng artifacts share ...`) and skill documentation; no new login system.

## Non-goals

* No public access to the artifact API, source, uploads, other versions, feedback, or the registry. A share exposes only the files copied into its own bundle.
* Shared pages cannot call back into HerdR (no cookies, no auth, opaque origin) and cannot load code from other hosts: bundles must be self-contained (vendor your JS/wasm). External CDN URLs are blocked and reported at approve time.
* No per-viewer editing/commenting through a link; no management web UI in this slice (CLI only); no email sending.
* No new public hostname (needs a cubert SNI change outside this repo); links live under `/s/` on `localdev.mcp.metabolomics.us`.
* No new identity provider and no API keys (the control plane stays open; the owner is the single operator).

## Anti-goals (failure even if it "works")

* Any path from the internet to the artifact registry, RustFS, or an unauthenticated control-plane route.
* Shared page script reaching the host origin's storage (the lab viewer keeps Cognito tokens in `sessionStorage` there).
* A lab link that opens for someone not in the pool or not on its list; a revoked/expired link that still serves files.
* A link whose content changes after approval.
* Leaking whether a token exists (every failure is the same 404).

## Constraints from the codebase (verified 2026-10-30)

* `collab-public` (internet-facing container behind the cubert SNI router and the bender nginx edge) deliberately has no database, AWS or blob access, and its edge allow-list 404s everything else. Keep it unchanged.
* Cognito auth today is a bearer access token verified by `CognitoAuthenticator` (RS256, issuer, `token_use=access`, client allow-list, `cognito:groups` re-checked from `members.json`, JWKS refreshed by `collab-sync.sh` every minute). `verify_id_token` exists for the verified email claim. No server sessions or cookies; login is client-side against `us-west-2gjtcm0pcp.auth.us-west-2.amazoncognito.com`. Pool `us-west-2_GjtcM0PCp` (shared with `lc-binbase-scheduler` and the collaboration edge); web client `herdr-collab-web` (code flow, no secret, scopes openid+email).
* Principals come only from `X-API-Key`; the open control plane makes everyone `operator`. `share` exists in the permission vocabulary but is enforced nowhere.
* Latest migration is `0035`; next is `0036`. `publications` (0004/0009) is a tailnet exposure controller and is NOT reused.
* Version outputs live in RustFS (hash-verified on download); report previews link to authenticated asset URLs.

## The design

### Share bundle (frozen at approve)

At APPROVE the control plane (which has registry + RustFS access) copies the chosen version's output files into a
per-share directory `/shares/<share_id>/files/<relative path>` on a Docker volume, verifying each file's pinned SHA-256,
and writes `manifest.json` (path, size, sha256, content-type, entry path). Rules:

* Entry: PDF → that file; HTML → `index.html` or the single `text/html` output or the output named by `--entry`; reports → their rendered preview (asset links shown as plain text, assets not included).
* Content types come from an allow-list by extension (html, css, js/mjs, json, csv, tsv, txt, svg, png/jpg/gif/webp, pdf, parquet, wasm, woff2); anything else → `application/octet-stream`. Paths are normalised; `..`, absolute paths, symlinks and duplicate case-folded names are refused.
* Caps (YAML, my defaults): 512 MiB per share, 20,000 files, 2 GiB total across live shares. Over the cap → approve fails with the numbers.
* The approve summary lists entry, file count, size, expiry, audience, and WARNINGS: HTML that references hosts other than the bundle itself, absolute paths, or a PDF/HTML entry that is missing.
* The volume is mounted read-write on control-plane-web/worker and READ-ONLY on the share container. Revoke/expiry deletes the directory (the DB row stays for audit).

### Data (migration `0036_artifact_shares.sql`)

* `artifact_shares`: `share_id`, `artifact_id`, `version_id`, `audience` (`lab`|`public`), `token_hash` (SHA-256 of a 256-bit random token; shown once), `allow_emails text[]`, `allow_groups text[]`, `state` (`pending`|`live`|`revoked`), `entry`, `file_count`, `total_bytes`, `title`, `expires_at`, `created_by`, `created_at`, `approved_by`, `approved_at`, `revoked_at`.
* `artifact_share_events`: append-only `share_id`, `at`, `kind` (`created`|`approved`|`opened`|`denied`|`revoked`|`expired`), `actor` (principal; Cognito sub/email for lab opens; `public`), `detail` jsonb with no tokens or bodies.
* **The internet-facing container has NO database access and no DB credential.** The database is the system of record; the control plane writes a projection onto the shares volume at approve, revoke and expiry:
  `/shares/by-token/<sha256(token)>/state.json` (`share_id`, `audience`, `state`, `expires_at`, `allow_emails`, `allow_groups`, `entry`, file index) and `/shares/<share_id>/files/...`. Token lookup is a path stat (hash only; constant-time irrelevant for a hash path), revoke flips/removes `state.json` so it takes effect on the very next request, and a sweep (every minute in the worker) deletes expired bundles. The container appends audit events as JSON lines to its own writable `/share-events/events.jsonl` (the only writable mount, size-capped and rotated); the worker ingests them into `artifact_share_events` (pattern of `collab-sync.sh`/`collab.jsonl`). A compromised container can therefore read only approved shares' metadata and files and forge audit lines, not touch the registry.
* Control plane and edge run on the same Docker host (bender), so a shared named volume works; the share container joins no network that reaches Postgres, RustFS or the control plane (like `collab-public`: published only to the edge).

### Owner API (control-plane-web, tailnet side; enforces the existing `share` permission via `report_permission`)

* `POST /api/v1/artifacts/{id}/shares` `{version_id, audience, allow_emails?, allow_groups?, expires_in_days?, entry?, title?}` → `pending` share (dry-runs the bundle and returns the summary and warnings; no token).
* `POST .../shares/{sid}/approve` `{confirm: true}` → copies the bundle, sets `live`, returns the URL ONCE (`https://localdev.mcp.metabolomics.us/s/<token>/`). Same-principal approval is allowed (single operator) but must be a separate confirmed call; the CLI prints what will be exposed first.
* `GET .../shares` (no tokens), `POST .../shares/{sid}/revoke`, `GET .../shares/{sid}/events`.
* Rules: lab shares need at least one of `allow_emails`/`allow_groups`; public shares cannot carry an allow-list; the version must exist and be rendered/valid.

### Public serving (new container `herdr-share-public`)

Same image, new flag `share_only` (pattern of `collab_only`): a minimal listener; the `/shares` volume read-only, a small writable `/share-events` volume, the Cognito `jwks.json`/`members.json` read-only, a signing secret from YAML; no database, RustFS, AWS or API routes. Routes (GET/HEAD/OPTIONS only):

* `GET /s/<token>/` (public) → redirect to `/s/<token>/f/<entry>`; lab → the static viewer page.
* `GET /s/<token>/f/<path>` (public) → the bundle file. Supports `Range` (206 incl. suffix `bytes=-N` and multi-range refused with 416 only if unsatisfiable, `Accept-Ranges: bytes`), `HEAD`, ETag = sha256 with `If-None-Match`/`If-Range`, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-cache` (revalidate by ETag, so revoke is still immediate), `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex`. CORS is needed because the sandboxed page has `Origin: null`: EVERY response (200, 206, 304 and the 404s) carries `Access-Control-Allow-Origin: *` and `Access-Control-Expose-Headers: Content-Range, Content-Length, Accept-Ranges, ETag`, and `OPTIONS` preflights are answered 204 with `Access-Control-Allow-Methods: GET, HEAD, OPTIONS`, `Access-Control-Allow-Headers: Range, If-Range, If-None-Match, If-Match`, `Access-Control-Max-Age: 600` (suffix ranges and conditional requests trigger preflights; the preflight cannot carry credentials, which is fine because the capability is the path). Relative URLs inside the bundle therefore work.
* Lab: the viewer page (`/s/<token>/` + static `/s/static/share.js`) runs PKCE code login against the Cognito hosted UI with redirect `/s/callback`, keeps tokens in `sessionStorage` (host origin), calls `POST /s/<token>/session` with the access token and `X-Id-Token`; the server verifies both (`CognitoAuthenticator`, client allow-list, verified email, allow-list or group, fresh membership, fail closed) and returns a short-lived (default 4 h, never past share expiry; when it lapses the page's fetches fail and the person reopens the link to sign in again — a sandboxed page cannot refresh it. The session URL is a bearer secret for that identity and share for its lifetime) session path `/s/<token>/k/<hmac-session>/f/`. The viewer then navigates to `<session path><entry>`. All bundle files are served under that base too, so relative URLs keep working; the session is an HMAC over (share_id, expiry, verified identity) with the YAML secret, stateless, so the container needs no DB writes beyond audit events.
* Content headers on EVERY non-PDF response (HTML, JS, CSS, SVG, JSON, text, octet-stream, wasm; an SVG without the sandbox would run script on the host origin): `Content-Security-Policy: sandbox allow-scripts allow-downloads; default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'`. With a configured bare https `public_url`, every fetch directive (`default-src`, `script-src`, `style-src`, `img-src`, `font-src`, `connect-src`, `worker-src`) also lists the share's own prefix `<public origin>/s/<token>/` next to `'self'`, because WebKit matches `'self'` against the sandbox's opaque origin and refused every fetch to the bundle (found by the browser harness); the prefix also covers the lab `k/<session>/f/` paths and no other share. Without one, `'self'` only (the Host header is never trusted). The CSP `sandbox` (without `allow-same-origin`) gives the page an opaque origin even when navigated to top-level, so shared script cannot read the host origin's `sessionStorage` or cookies, and its `fetch` to the bundle works through the permissive CORS above (capability = the path). PDFs are served `Content-Type: application/pdf`, `inline`, with `nosniff` and only `frame-ancestors 'none'` (a sandbox or strict `default-src` can break browsers' built-in PDF viewers). Verified in Chrome, Firefox and Safari during the smoke test; the fallback is `Content-Disposition: attachment`.
* Every failure — unknown/revoked/expired token, wrong user, stale membership, missing file, bad session — is the same 404; the reason goes only to `artifact_share_events` (`denied`, rate-limited) and a rate-limited log.
* Edge (`collab-edge/nginx.conf` renderer): an upstream for `herdr-share-public`, a `^~ /s/` location (GET/HEAD, its own `limit_req` zone, no request body, proxy buffering off for large files, large `proxy_read_timeout`), `access_log off` for `/s/` (tokens and session HMACs are in the path; the app logs only a hash prefix); everything else keeps the current allow-list. The share slice renders `control_plane.share: {enabled, max_share_bytes, max_total_bytes, max_files, default_public_days, session_seconds (14400), signing_secret}`; Cognito values come from the existing `control_plane.collab` slice.

### Owner experience

`herdr-eng artifacts share create|approve|list|events|revoke ...`; `--audience lab --email a@x --email b@x [--group artifacts-users]`; `--audience public --expires-in-days N`; `--entry path`. The `herdr-artifact` skill gets a share reference page and an updated safety contract (shares are pinned, revocable, approve-gated, bundles self-contained).

### Deploy order

1. Migration (idempotent) and the shares/share-events volumes.
2. Control plane web/worker with the owner API and the `/shares` volume.
3. `herdr-share-public` container + edge regeneration via `deploy.sh`.
4. Cognito: add `https://localdev.mcp.metabolomics.us/s/callback` (callback + logout) to `herdr-collab-web`, then smoke tests: private, public (PDF, and an HTML + parquet range-loading bundle), lab with the owner's own account, revoke, expiry.

## Decided by the operator

Three audiences (private default, lab = the `lc-binbase-scheduler` pool, public); one document to three lab people and one public artifact to an external user tomorrow; artifacts may be PDF, HTML, or HTML + parquet collections loading dynamically; the full feature, not a first slice; no API keys; reuse the existing pool and edge.

## My calls (change any)

* Links under `/s/` on the existing public host; separate `herdr-share-public` container; new `artifact_share*` tables.
* Frozen file-bundle copy on a read-only Docker volume (not bytea, not live RustFS); caps 512 MiB / 20,000 files per share, 2 GiB total.
* Scripts allowed but sandboxed to an opaque origin with same-bundle-only network (`connect-src 'self'`); no external CDNs.
* Lab sessions are stateless HMAC path tokens (1 h) because relative URLs and parquet range requests cannot carry a bearer header.
* Named emails authorised by the verified id-token email alone; groups need fresh `members.json` (stale > 10 min denies). The owner confirms the three people have pool accounts before approving (CLI checks via AWS when it can).
* Public expiry required (30 d default, 1–90); lab expiry optional.
* Single-operator approval via a separate confirmed call.
* The public container gets no database at all (file projection + read-only volume + append-only events file); this replaces revision 2's `share_reader` role.
* Approve enumerates files from the version's `output_manifest` (names may be nested; names validated at approve; entries lacking `size_bytes`/`media_type` are measured after the hash-verified download; entries without a hash are refused); the version must be `ready` (reports: also rendered); reports have their preview generated by the renderer at approve and stored as `preview.html`; the approve step re-runs the scoped-blob/ACL check so a share cannot include a blob the artifact doesn't own.
* The `OPTIONS` method is allowed on `/s/` only for the CORS preflight above.

## Known risks to test before launch

* A CSP `sandbox` without `allow-same-origin` makes `localStorage`/`sessionStorage`/IndexedDB/cookies throw and only `blob:` workers are reliable; some parquet/wasm libraries probe those or spawn workers from a URL. The smoke test uses the operator's REAL parquet library and artifact. If it fails there is no safe fix inside this design (a separate origin is out of scope), so we stop and decide with the operator.
* `connect-src 'self'` does not stop exfiltration by navigation; acceptable because shared bundles are authored by the operator, and the approve summary says so.
* `'self'` and module-script/worker fetches from an opaque origin differ across browsers: the smoke test runs in Chrome, Firefox and Safari.

## Needs the operator (outside the code)

* Adding the `/s/callback` URL to the Cognito client `herdr-collab-web` (a dedicated client, additive) — at deploy time, with the operator's go-ahead. If the client cannot serve this flow, stop and report rather than creating another login system.
* Every production step (migration, role creation, rollout, edge reload) happens only with the operator's go-ahead at that moment.

## Left to the builder

Module layout, exact SQL, viewer JS, nginx snippet details, volume/mount names, test structure, CLI wording, rate-limit numbers, how `share_only` is wired into compose, the extension→content-type table details, how the approve step enumerates a version's output files from the registry.
