# Artifact host cutover (member-owned artifacts)

Moves member artifacts from the control plane to the dedicated `herdr_artifact_host`. Spec: `docs/superpowers/specs/2026-11-06-artifact-host-app-design.md`
("Migration and cutover"); deploy details: `deploy/control-plane/COLLAB.md` ("Artifact host"). Run by the owner or an
operator on the deploy machine (bender), from the repo checkout. Every `<...>` is filled in from your fleet config; no
secret is ever pasted into this document or a shell history (read it from the fleet YAML into a file with `umask 077`).

This runbook is not automated and has not been run. The code and tests are merged to `main` first; nothing below happens
before that PR is green and merged.

## What actually happens (read first)

`deploy.sh` with `control_plane.artifact_host.enabled: true` brings the host database, roles, migration, public/internal/relay
containers up BEFORE the control plane and the edge, and renders the edge to route the host name to the host in the SAME
run. So there are two deploys and one decision:

1. Deploy A (host disabled, the default): new release only; the host is not running, the edge is unchanged. This is the
   "dark" step: nothing member-facing changes.
2. Preflight (below) tells you whether the control plane holds any member data. Production held 0 member artifacts and
   0 library items at design time (77 artifacts, all API-key CI/agent reports; 4 share rows); members were disabled, so
   nothing can have been written to the old path.
3. Deploy B (host enabled, share disabled): host up, then edge switched. Import and verify run immediately after, and
   only while the host has taken no member writes. If the preflight shows member rows, STOP and ask the owner: the
   import must then run between "host up" and "edge switched", which `deploy.sh` does not offer (see Stop conditions).

Import and `verify --exact` are pre-cutover tools: once the host takes real writes, a difference means "changed since
import", not corruption.

## 0. Preconditions

- [ ] The branch PR is merged to `main` with CI green (lint, type-check, tests, build) and `/code-review` findings fixed.
- [ ] `git fetch origin && git rev-parse origin/main` is the commit you intend to ship; write it down (rollback target
      is the commit running today: `git -C <repo> rev-parse HEAD` on the deploy checkout, or the tag of the running
      `herdr-cp-*` image).
- [ ] Fleet config (`~/.config/herdr-engineering/fleet-config.yaml`, or `--fleet-config`) edited on bender with the new
      section drafted but NOT yet enabled: `control_plane.artifact_host.enabled: false` (or absent).
- [ ] The edge certificate names the host's `public_host` (deploy.sh checks and refuses otherwise). `public_host` today is
      the share name (`artifacts.local`); the host takes over that name, so the cert already covers it.
- [ ] Disk: room for two dumps and the host blob volume (`df -h` on the docker data dir).
- [ ] Docker and the existing stack are healthy: `docker ps --filter name=herdr-cp --format '{{.Names}} {{.Status}}'`
      shows every container `healthy`.
- [ ] The owner is reachable. Nothing below touches DNS or retires the old host.

## 1. Back up

Control-plane database (container `herdr-cp-postgres`, database `herdr`; the user is `HERDR_CP_PG_USER` from the deploy env, never typed here;
the dump is 0600):

    umask 077; mkdir -p ~/herdr-backups/artifact-host-cutover
    docker exec herdr-cp-postgres pg_dump -U "$HERDR_CP_PG_USER" -d herdr -Fc \
      > ~/herdr-backups/artifact-host-cutover/cp-$(date -u +%Y%m%dT%H%M%SZ).dump
    ls -l ~/herdr-backups/artifact-host-cutover/       # expect a non-zero size
    pg_restore --list ~/herdr-backups/artifact-host-cutover/cp-*.dump | head   # expect a table of contents, no error

Host database (only once the host exists; deploy B also writes `pre-<tag>-<time>.dump` itself under
`~/.config/herdr-engineering/artifact-host/backups/` before migrating, and stops the deploy if that backup fails):

    umask 077
    REPO=<the repo checkout on bender, at the release being deployed>
    TAG=$(docker inspect --format '{{.Config.Image}}' herdr-artifact-host-artifact-host-public-1 | cut -d: -f2)   # the running host image tag
    HERDR_ARTIFACT_HOST_TAG=$TAG \
    HERDR_COLLAB_SYNC_DIR=<the collab sync dir deploy.sh uses: ~/.local/share/herdr-engineering/collab-sync> \
    HERDR_ARTIFACT_HOST_DIR=$HOME/.config/herdr-engineering/artifact-host \
    docker compose -p herdr-artifact-host -f $REPO/deploy/artifact-host/compose.yaml exec -T artifact-host-db \
      pg_dump -U artifact_admin -d artifact_host -Fc > ~/herdr-backups/artifact-host-cutover/host-$(date -u +%Y%m%dT%H%M%SZ).dump

The compose file requires all three variables (`HERDR_ARTIFACT_HOST_TAG`, `HERDR_COLLAB_SYNC_DIR`,
`HERDR_ARTIFACT_HOST_DIR`) for any command; `deploy.sh` sets them itself, and nothing prints a ready-made helper for
by-hand use except the exit-3 recovery steps. Use the checkout path, never a temporary directory. Blob storage of the control plane (RustFS bucket) is not modified by
anything here; the importer only reads it.

## 2. Deploy A: new release, host dark

    deploy/control-plane/deploy.sh --ref origin/main --dry-run
    deploy/control-plane/deploy.sh --ref origin/main

Expected: dry run prints no secret and ends without error; the deploy finishes with the usual health check and exit 0.
Check: `docker ps --filter name=artifact-host` lists nothing (host not running) and
`curl -sS -o /dev/null -w '%{http_code}\n' https://<public_host>/s/probe/` still answers as before (the legacy share
path: 404 for an unknown token). If the deploy fails it rolls itself back; do not continue.

## 3. Preflight: does production hold member data?

Read-only, on the control-plane database (if the table name differs, find it with `\dt *artifact*`):

    docker exec -i herdr-cp-postgres psql -U "$HERDR_CP_PG_USER" -d herdr -v ON_ERROR_STOP=1 -c \
      "SELECT count(*) AS member_artifacts FROM artifacts WHERE created_by LIKE 'member:%';"

Expected: `0` (or near-empty). If it is nonzero, do not continue to deploy B; see Stop conditions.

## 4. Enable the host in the fleet config

Edit the fleet YAML (never print values):

    control_plane:
      share:        { enabled: false }          # host and share cannot both be enabled; the host serves /s/ itself
      artifact_host:
        enabled: true
        public_host: <the member/share host name>
        mcp_public_hosts: [<mcp name agents use>]   # optional; routes POST /mcp/artifacts to the host
        session_secret: <>=32 bytes, >=10 distinct characters>   # openssl rand -hex 32
        claim_key:      <>=32 bytes, different from session_secret>
        db_password:    <>=16 chars>
      artifact_library:
        host_key: <the dashboard's bearer key; only its sha256 reaches the host>   # the internal key
        host_url: http://artifact-host-relay:8091                                   # exactly this
      collab: { ... cognito_pool, cognito_clients, cognito_web_client, cognito_domain, cognito_group ... }

`control_plane.api_keys` is NOT required for the host path. Remove the legacy `control_plane.collab.members` section (or
set `enabled: false`) in the same edit: while the host is enabled it is ignored everywhere (fleet validation, the
collab slice, the dashboard and worker startup read nothing from it), but leaving it in is confusing and invites
someone to re-enable the old path. The share-public path (host disabled) still requires share, the bridge and `api_keys`.

    herdr-eng fleet config validate         # expect revision and host count, no error
    deploy/control-plane/deploy.sh --ref origin/main --dry-run   # renders slices and compose, prints no secret

## 5. Deploy B: host up, edge switched

    deploy/control-plane/deploy.sh --ref origin/main

Expected order in the output: certificate check, image build, database up, roles, pre-migration dump, migrate, host
containers `--wait` healthy, control plane, edge, then the probe of `https://<public_host>/s/probe/` answering 404
(a WARNING line instead means the edge did not route to the host: stop and use Rollback). Exit code 3 means a manual
rollback is required (see Rollback).

## 6. Import and verify (right after deploy B, before any member writes)

The importer needs the control-plane DSN and the host's migrate slice, and the host networks are isolated. Run it in a
one-off container on the host image, attached to the host DB network and to the control-plane DB network, using a
read-only control-plane role created for this (a role with `SELECT` only on the library/artifact tables; drop it after):

The container runs as uid 10001 with a read-only root, so every mount must suit that uid. Prepare them first:

    OUT=~/herdr-backups/artifact-host-cutover/out
    mkdir -p $OUT && sudo chown 10001:10001 $OUT && sudo chmod 0700 $OUT      # /out: the manifest is written here by uid 10001
    # (the migrate slice is a 0644 file inside a 0700 directory: the daemon mounts the file, and uid 10001 can read the file itself)
    # /src: the control plane's blob directory must be readable by 10001. Check: `sudo find <dir> ! -perm -o+r | head`
    # prints nothing. If it prints files, copy instead of widening the original:
    #   sudo cp -a <dir> /srv/ah-import-src && sudo chown -R 10001:10001 /srv/ah-import-src   (remove it afterwards)
    # pgpass (only if the read-only role has a password): a 0400 file owned by 10001, MOUNTED below; PGPASSFILE means nothing otherwise
    printf '%s\n' 'herdr-cp-postgres:5432:herdr:<ro-role>:<password>' | sudo tee /srv/ah-pgpass >/dev/null
    sudo chown 10001:10001 /srv/ah-pgpass && sudo chmod 0400 /srv/ah-pgpass     # remove it afterwards

    docker run -d --name ah-import --read-only --cap-drop ALL --user 10001:10001 \
      --network herdr-artifact-host_artifact-host --entrypoint sleep \
      -v ~/.config/herdr-engineering/artifact-host/migrate.json:/run/secrets/m:ro \
      -v herdr-artifact-host_artifact-host-blobs:/var/lib/artifact-host/blobs \
      -v <blob-dir-or-/srv/ah-import-src-or-s3-config-file>:/src:ro -v $OUT:/out \
      -v /srv/ah-pgpass:/run/secrets/pgpass:ro \
      herdr-artifact-host:<tag> 3600
    docker network connect <cp-db-network> ah-import       # the control plane's postgres network (`docker inspect herdr-cp-postgres`)
    docker exec -e PGPASSFILE=/run/secrets/pgpass ah-import herdr-artifact-host import --config /run/secrets/m \
      --manifest /out/manifest.json --source-dsn 'postgresql://<ro-role>@herdr-cp-postgres/herdr' \
      --source-blob-dir /src      # or --source-s3-config /src
    ls -l $OUT/manifest.json      # exists; the importer writes it inside the commit, so no manifest means nothing was imported

Expected: `import:` lines ending in a summary with `0` (or near-empty) rows and blobs; exit 0 (the command also runs
verify). Then:

    docker exec ah-import herdr-artifact-host verify --config /run/secrets/m --manifest /out/manifest.json --exact   # prints from /out

Expected: `verify: identical to the manifest`, exit 0. A nonzero exit is a failed verify: do not continue; Rollback.
Re-running import is safe (idempotent). Keep `manifest.json` with the backups. Then `docker rm -f ah-import`, remove `/srv/ah-pgpass` and any `/srv/ah-import-src` copy, and drop the temporary read-only role. (Check network, volume and mount names against `docker network ls` / `docker volume ls` before running. With a near-empty source this is a formality, but the manifest is the audit record.)

## 7. Live checks (all must pass)

Substitute `H=<public_host>`, `M=<mcp host>`, `DASH=<dashboard base URL>`.

| # | Command | Expected |
|---|---------|----------|
| 1 | `curl -sS -i https://$H/api/member/config` | `200`, JSON with the Cognito client/domain, no secret |
| 2 | `curl -sS -i https://$H/s/unknown/` and `curl -sS -i https://$H/s/other-unknown/` | both `404`, byte-identical bodies (`diff <(curl -s https://$H/s/unknown/) <(curl -s https://$H/s/other-unknown/)` prints nothing) |
| 3 | Open a real share link in a browser (or `curl -sSi` it) | landing bar present; response has `Content-Security-Policy` (claim CSP); content is served inert |
| 4 | From the dashboard: open `$DASH/library` signed in as the operator | lists items (empty is fine), page loads; this proves the internal key and the relay. A POST to the dashboard's library route answers `501` |
| 5 | From the dashboard container, send a POST to the relay: `docker exec herdr-cp-web python -c "import urllib.request as u; u.urlopen(u.Request('http://artifact-host-relay:8091/v1/feed', data=b'', method='POST'))"` | fails with `HTTPError: HTTP Error 403` (the relay allows GET only; no key needed to see the refusal). A `200` or `405` here is a failure of the relay config |
| 6 | `docker exec herdr-artifact-host-artifact-host-public-1 python -c "import socket,sys; socket.create_connection(('herdr-cp-web', 8080), 3); sys.exit('REACHABLE')"` (container name per `docker ps`) | exits non-zero with `socket.gaierror` (name does not resolve) or a timeout/`OSError`: the host cannot reach the control plane. It FAILS the check if it prints `REACHABLE` or exits 0 |
| 7 | Browser: `https://$H/` sign in with Cognito, land on the member page, reload, sign out | round trip works. The member session is a bearer token the page keeps and sends in the `Authorization` header (never a cookie): in the browser's network tab `/api/member/*` calls carry `Authorization: Bearer ...`, and no `Set-Cookie` appears. After sign-out the same call with the old token answers `401` |
| 8 | Agent: `POST https://$M/mcp/artifacts` (`tools/call` create_artifact with a member's agent token) | lands `pending` (visible in the member page awaiting accept; not shared); after the member accepts, it appears |
| 9 | `$DASH/library` after step 8 and after accepting | the item shows read-only (no edit/share controls; a POST to the internal API still 405) |
| 10 | `docker ps --filter name=artifact-host --format '{{.Names}} {{.Status}}'` | db, public, internal, relay all `healthy`; no published ports |

Record the output of each in the report. Any failure: Rollback, then investigate.

## 8. Report to the owner, then stop

Send: shipped commit, backup paths, manifest summary, results of checks 1-10. **Stop here.** DNS changes and retirement of
the old host/share path wait for the owner's explicit go-ahead. The old rows stay in the control plane untouched until the
owner confirms removal (a later PR deletes the dead control-plane code).

## Rollback

The host's data is additive and the control plane's copy is unchanged, so rollback is a routing change.

1. If the host has taken real member data (anything created after deploy B), dump the host DB FIRST (command in section
   1). Rolling back loses what was written to the host since cutover unless you re-import it by hand; say so to the owner.
2. Point the edge back: set `control_plane.artifact_host.enabled: false` and `control_plane.share.enabled: true` (the previous
   share settings), then redeploy THE SAME `origin/main` commit: `deploy/control-plane/deploy.sh --ref <the commit you shipped>
   --no-fetch --allow-divergent`. Never roll back to a commit from before this branch: those releases have no host files,
   so the deploy cannot stop the host cleanly or restore its roles (it would record a manual-rollback case), and their fleet
   validation does not know the `artifact_host` section. The shipped commit contains both paths; the flag decides. The
   deploy stops the host containers (volumes are kept) and renders the share edge again. Re-run check 2 and the legacy probe.
3. `deploy.sh` exit 3 ("FAILED TO ROLL BACK" / manual steps printed): it refuses to silently revert a host whose schema it
   changed. Follow the printed steps exactly: stop the host roles, `pg_restore` the pre-migration dump
   (`... exec -T artifact-host-db pg_restore -U artifact_admin -d artifact_host --clean --if-exists < <dump>`), then run
   the printed redeploy command (in this cutover the previous release is the Deploy A commit, the same `origin/main` commit).
   The printed `docker compose` lines use the repo checkout's compose file and carry all three required variables; run them
   from that checkout at the shipped commit. Take a host DB dump before restoring.
4. The control-plane DB backup from section 1 is only needed if the control plane itself was damaged; restore it with
   `pg_restore --clean --if-exists` after stopping the control plane, never otherwise.

## Stop conditions (ask the owner)

- Preflight count is nonzero (members have data on the old path): the import must run before the edge switch, which
  `deploy.sh` cannot do in one run. Needs a split (bring up only the host db/migrate by hand, import, verify, then deploy B).
- Verify fails, a live check fails, or the host DB / blob volume is unexpectedly non-empty before the import.
- Any step wants a production secret pasted into a command line or chat.
- Anything about DNS, certificates for a new name, or retiring the old host: owner only.
