# Cutover to the central fleet config

This runbook moves a running control plane from the old
`~/.config/herdr-engineering/control-plane.env` setup to the central fleet config:
one file, alone in `~/.config/herdr-engineering/fleet/`.
Do the steps in order, all on the control-plane host (bender) unless a step says
otherwise. No step prints a secret.

## Safe order

1. **Create the central file from the old env file.** This keeps the Postgres
   and MinIO credentials. Postgres applies `POSTGRES_*` only to an empty volume,
   so new credentials would lock the server out of its own database, and it would
   quietly fall back to in-memory storage.

   ```bash
   herdr-eng fleet config init --from-env ~/.config/herdr-engineering/control-plane.env
   ```

   The command reports the variable names it imported, the names it did not
   recognise, and the line numbers it skipped. It never shows a value. It prompts
   only for what is still missing:
   - public URL
   - LLM upstream address (and key, if `FABRIC_LLM_UPSTREAM_KEY` was not in the file)
   - CI address and token
   - the host list

   **GitHub and CI tokens.** The old stack kept them in fixed files that the env file
   never named: `~/.config/herdr-engineering/github-token` and `.../ci-token`.
   `--from-env` reads those files when the env file names no token file, and prints
   only `github.token: imported` / `ci.token: imported`, or `missing`. When the
   GitHub token is missing, it asks for it; leave it blank (or run without a
   terminal) to keep it empty. Without it the GitHub monitor and CI checks have no
   token, so check that line before you go on.

   When asked for the **web bind address**, answer with this machine's tailnet IP
   (`tailscale ip -4`) unless the old file already set `HERDR_CP_WEB_BIND` to it.
   Use the Hosts-page names as host ids: `bender`, `beast`, `mac.lan`.

   To run without prompts, put the remaining answers in a 0600 YAML file and pass
   `--answers FILE`.

   The command writes `~/.config/herdr-engineering/fleet/fleet-config.yaml` (0600,
   in a 0700 directory) and leaves a symlink at the old
   `~/.config/herdr-engineering/fleet-config.yaml`. API keys stay off
   (`control_plane.api_keys: {}`), exactly as today.

2. **Validate.**

   ```bash
   herdr-eng fleet config validate
   ```

   It prints `ok revision=... hosts=N`. It also checks that `pg_dsn`, `minio_*` and
   `compose.HERDR_CP_{PG,MINIO}_*` name the same credentials.

3. **Dry-run the deploy.** With collab enabled, first check that
   `~/.config/herdr-engineering/collab/` holds nothing but `collab-config.json`. That
   directory is now mounted whole into collab-public, and `deploy.sh` refuses to run
   if it holds anything else.

   With collab enabled the central file also needs, by hand (`init --from-env` does not
   create a `collab` block): `control_plane.collab` with the Cognito ids and
   `operator_user` (the Cognito username the dashboard's no-sign-in collaboration acts as;
   the old env file's `HERDR_COLLAB_OPERATOR_USER`, optional `operator_email`), and
   `control_plane.compose.HERDR_COLLAB_CERT_DIR` set to the directory collab-sync copies the
   edge certificate into (main's default was
   `~/.local/share/herdr-engineering/collab-edge-certs`). `HERDR_COLLAB_OPERATOR_PORT`
   (default 9482) is optional.

   ```bash
   deploy/control-plane/deploy.sh --dry-run
   ```

   The dry run:
   - renders the compose variables, the llm-proxy slice and (with collab on) the
     collab slice into a temporary directory
   - runs `docker compose config -q`
   - changes nothing on the host

4. **Deploy.**

   ```bash
   deploy/control-plane/deploy.sh
   ```

   It builds `herdr-engineering:<sha12>` and recreates control-plane-web,
   control-plane-worker and llm-proxy on that tag (plus collab-public and
   collab-edge when collab is enabled).

   **This first deploy has no automatic rollback.** The commit running now
   predates the directory-mounted fleet config, so its compose files cannot run
   from the new file. `deploy.sh` says so before it starts.

   If the health check fails, roll back by hand. Keep `control-plane.env` in place,
   then run the previous commit's own `deploy.sh`, which reads `control-plane.env`.
   `deploy.sh` prints this command with the exact commit:

   ```bash
   git -C ~/IdeaProjects/herdr-engineering worktree add /tmp/herdr-rollback <previous-commit>
   /tmp/herdr-rollback/deploy/control-plane/deploy.sh --ref <previous-commit> --no-fetch
   ```

   The old `deploy.sh` recreates **only control-plane-web**. The worker and the
   llm-proxy stay on the new image, still reading the central file's directory and
   the llm slice, which remain on disk. To put them back on the old setup as well,
   run the old commit's compose from the same worktree with the old env file:

   ```bash
   cd /tmp/herdr-rollback/deploy/control-plane
   HERDR_CP_ENV_FILE=~/.config/herdr-engineering/control-plane.env HERDR_CP_IMAGE_TAG=<previous-sha12> \
     docker compose --env-file ~/.config/herdr-engineering/control-plane.env -p herdr-control-plane \
     -f compose.yaml up -d --no-deps --no-build control-plane-worker llm-proxy
   ```

   The previous commit is in the running image's tag (`docker inspect -f
   '{{.Config.Image}}' herdr-cp-web`) and in its receipt under
   `~/.local/state/herdr-engineering/deploys/`.

   Every later deploy rolls back automatically. It uses the commit recorded in the
   running image's receipt, with that commit's compose files. It prints "rolled
   back ... and healthy" only when the old commit answers health again. Otherwise it
   prints FAILED TO ROLL BACK with the manual command and exits 3.

5. **Give each host its token.** Run these on the control-plane host:

   ```bash
   herdr-eng fleet config token bender --target local       # this machine
   herdr-eng fleet config token beast                        # over ssh
   herdr-eng fleet config token mac.lan --target <mac-ssh-target>
   ```

   (`herdr-eng fleet join <ssh-target> --label <name>` does the same for a new
   machine and adds it to the file.)

   Each command writes `~/.config/herdr-engineering/host.json` (0600) on the host.
   The token travels on stdin only, and only its hash is stored centrally. Open the
   Hosts page: each card should show a config badge. If a card shows no badge, its
   host id does not match the Hosts-page name.

6. **Roll the host agents.**

   ```bash
   deploy/deploy-fleet-agents.sh
   ```

   A host without `host.json` is skipped with a hint, and the run exits non-zero.

7. **Turn on the inspector and verify.** Set `inspector.enabled: true` in the
   central file, either by hand edit or atomic replace; the server picks it up on
   its next read. Then start a **new** pi session on a host and check that the
   session page shows inspector data.

8. **Remove `control-plane.env`.** Do this only after everything above is verified
   *and* the operator confirms. Until then it is the manual-rollback path from
   step 4.

**Migrations and rollback.** The worker applies SQL migrations when it starts. A
rollback puts the old worker image back on a schema the new image may already have
migrated. Keep migrations backward compatible (additive: new tables and nullable
columns, no drops or renames in the same release), or plan a manual restore.

## Wrong orders that break

- **New host code before the new control plane.** The old control plane has no
  `/api/v1/fleet/config` and answers 404. Hosts now treat 404 (and 403) like an
  outage and run on their cached configuration. On a first start there is no
  cache, so the daemon refuses to start and says why. Deploy the control plane
  (step 4) before rolling agents (step 6).
- **Turning on API keys on day one.** The browser dashboard, the llm-proxy and the
  emit hook do not send `X-API-Key` yet. Filling `control_plane.api_keys` (or using
  `init --with-api-keys`) makes them all fail with 401. Leave it `{}`.
- **A fresh `init` without `--from-env` on an existing control plane.** It
  generates new Postgres and MinIO users that the live volumes do not have.
- **Putting other files next to the central file.** `deploy.sh` mounts that
  directory into the containers and refuses to run if it holds anything else.
  Backups go to `~/.config/herdr-engineering/fleet-backups/`.
- **Deleting `control-plane.env` before step 7 is verified.** That removes the
  only rollback for the first deploy.

## Day-two operations

- **Revoke a host:** `herdr-eng fleet config revoke <host>`. It takes a dated
  backup first, and the host's token stops working on the server's next read.
- **Rotate a host's token:** `herdr-eng fleet config token <host> [--target ...]`.
- **Hand edits:** any editor works, because the directory mount sees a
  rename-replace. An invalid edit is rejected, and the last good revision keeps
  serving (`/api/v1/fleet/config/status` shows `last_error`). The tools that
  rewrite the file drop YAML comments.
