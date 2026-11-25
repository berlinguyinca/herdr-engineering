# Changelog

All notable changes to HerdR Engineering are documented here. Dates are
YYYY-MM-DD. The format follows Keep a Changelog.

## [Unreleased]

### Fix: deploy compose mounts, subnet overlap and image pins (bug-hunt)

- `fabric-gateway` and `herdr-web` no longer bind-mount `../fabric-stack/config.yaml` (it resolved into deploy.sh's deleted temporary worktree, so a container restart came back without its config). `deploy/fabric-stack/config.yaml` is baked into the image as `/app/config.yaml` and keeps the memory routes off. The memory routes live outside git in `~/.config/herdr-engineering/fabric/memory-route.yaml` (only `fabric.local_routes`, which the gateway appends to the baked `fabric.routes`, so later changes to the shipped routes still reach the host); `fabric-gateway` mounts the directory read-only and lists the file third in `HERDR_ENGINEERING_CONFIG=/app/config.yaml:/fabric-local/memory-route.yaml` (several `:`-separated files are accepted, later ones win, a missing one is skipped with a warning; a single path that itself contains `:` is used whole). `deploy.sh` and `fabric-stack.sh up` create the directory as the deploying user and refuse one owned by someone else. `scripts/fabric-stack.sh memory-route enable [PORT]` writes the file atomically and fails loudly if it cannot, `disable` removes only it, and both print `docker restart herdr-fabric-gateway` as the apply step. **Before the first deploy of this change, a host that has memory routes enabled must run `scripts/fabric-stack.sh memory-route enable <port>` once** (e.g. 8897 on bender); otherwise the deploy brings the gateway up routes-off and `memory.dev.lan` / `memory.local.metabolomics.us` stop resolving. `test_recreated_services_have_no_relative_bind_mounts` now covers every service in deploy.sh's `APP_SERVICES`.
- The private web-egress default for hosts with no approved egress network moved from `172.31.252.0/29` to `172.31.252.32/29` (gateway .33, source .34): the old one sat inside the artifact host's fixed `172.31.252.0/28` edge network, so it could never be enabled beside it. A host that already has an approved egress network keeps its subnet: an ordinary deploy changes nothing, and `deploy.sh --memory-private-egress --memory-private-egress-gateway <ip>` re-uses the approved CIDR (to stay on `172.31.252.0/29` nothing needs to be passed). To move to the new range, run `--no-memory-private-egress` first, then enable with `--memory-private-egress-cidr 172.31.252.32/29`; naming a different CIDR while the old network exists is refused. A test asserts the fixed subnets are pairwise disjoint.
- The fabric image base (`python:3.12-slim`), `postgres:16-alpine`, `coredns:1.11.4` and the memory-client base are pinned by digest, and the runtime image installs under `deploy/fabric-stack/constraints.txt` exported from `uv.lock`. A test keeps the file equal to `uv export --locked` (so a pin bumped only in `pyproject.toml`, without `uv lock`, fails CI); `uv==0.13.7` is in a new test-only `ci-tools` extra (installed by `scripts/ci/unit-gates.sh`, not by `install.sh`; the test skips outside CI when uv is absent). Versions are unchanged. `minio/minio` stays pinned by exact tag only (its digest cannot be resolved from Docker Hub).
- Operator-visible: the digest-form image strings differ from the old tags, so the first full `docker compose up -d` (`scripts/fabric-stack.sh up`) recreates `postgres` and `coredns` once, and `postgres` may come up on a newer 16.x patch release than the one it ran before.
### Fix: artifact host storage is reclaimed, and disabled Cognito accounts are refused per request

- `herdr_artifact_host` housekeeping gains a `versions` duty that collects blobs only for a *tombstoned* artifact whose `purge_after` hold has passed: its versions are marked `purged` and the blobs only they used are deleted (row and file) under publish's per-sha lock, with the artifact row locked as `share_create` does. A tombstone with no `purge_after` (an imported one) gets the standard 30-day hold counted from the first pass that sees it, written back, and is not purged on that pass; `legal_hold` artifacts, versions a pending or live share points at, and blobs a kept version still names are never touched. Old versions of a live (or retracted) artifact are kept, and now count toward the member's quota: publish adds the superseded version to `bytes_used` instead of replacing it, delete/discard settle the whole held size, and the usage reconciliation sums current plus kept old versions. The orphan-blob sweep (one-hour `blob_grace`, for upload blobs no record references) is unchanged and ignores purged versions' manifests. `SweepReport` gains `versions_purged`.
- `SyncedDirectory.groups` returns no groups for an account listed under `disabled` in `members.json` (the sync still lists it under its group), so a disabled account's member session and agent tokens are refused on the next recheck instead of waiting for housekeeping's revocation pass.
### Fix: gateway lease and pipe bug-hunt findings

- `FabricGateway.op_register` validates every lease `routes[].host` with `validate_target_host` (same SSRF rule as `target_host`), including re-registration of an existing lease id.
- `rewriting_pipe` (TLS / header-rewrite path) is bounded: the request head must arrive within the inactivity timeout, both sockets get TCP keepalive (also on accepted clients in `_proxy_one`), and the pumps end on whole-connection idleness.
- `proxy_pair` / `prefixed_pipe` use one shared inactivity clock for both directions and no longer half-close the peer on a recv timeout, so healthy one-way streams (SSE, downloads) are not cut at 30 s / 70 s.
- `DevServiceRegistry.heartbeat` refuses to revive an expired lease whose external port was re-allocated (the agent re-registers); idempotent `register` refreshes `expires_at`, moves a lapsed lease off a taken port, and runs under the registry lock.
- A failed send on one pump direction no longer stops the opposite direction, so an origin that answers early and closes still has its response delivered; ETIMEDOUT from TCP keepalive is no longer mistaken for a poll timeout, and TCP keepalive idle is 15 s (was 60 s, longer than the idle timeout).
- `rewriting_pipe` waits up to `HERDR_GATEWAY_RESPONSE_TIMEOUT` seconds (default 300) for the origin's response head and answers 504 on expiry; local (gateway-less) lease renewal re-registers a lease whose port was re-allocated.
- A backend that stops accepting bytes (no write progress for the inactivity timeout) ends a `proxy_pair` / `prefixed_pipe`; a backend that reads a long upload and answers only at the end is not cut. The head deadline always answers 504, an origin that closes or resets before any head byte answers 502 (never an empty reply), and a dead origin (keepalive ETIMEDOUT) answers 502.
- Gateway-less renewal: a lease moved to a new port restarts its forwarder (`enroll --serve`, `enroll-dir --serve`), `devfabric heartbeat` re-registers like the serve loops, and `heartbeat`/re-register reload the store first so a port another CLI process took is seen. `HERDR_GATEWAY_RESPONSE_TIMEOUT` must be finite and positive, else the 300 s default applies.
- `enroll --serve` tracks the bound port itself (`_renew_serving_origin`) instead of comparing against the enroll-time lease object, so a lease that moved port restarts its forwarder once, not every 30 s cycle.
- `DevServiceRegistry.reload_if_changed` checks, reads and swaps under the registry lock and keeps in-memory health fields, so a threaded gateway cannot lose a lease to an older read. `enroll-dir --serve` renewal logs and retries a forwarder that failed to bind (the moved port is recorded only once bound) and one lease that cannot be re-registered no longer stops the others renewing. `--route` help and docs say route hosts must be IP literals or localhost.
- Fabric gateway token, review round 4: a client whose host-config fetch fails sends no bearer instead of failing (the gateway stays strict); `promote`/`demote` reset the HA counters; a tokened standby takes over from an active peer that is closed or unreadable; the agent no longer pins the host-config token after a config change; `dev` gateway probes report a rejected token as reachable; `fabric-stack.sh status` sends the bearer. A token file at the default path beats host-config `fabric.token`, so remove stale files before rollout.

- Fabric gateway token, review round 3: a host with no host.json is "no token" again (the memoised failure keeps `permanent`), so `--allow-open` serves; the host-config memo is race-free; a whitespace-only token file is "no token"; a negative or non-numeric Content-Length and a non-object JSON body are 400; HA with differing tokens ends with exactly one active node (`/healthz` carries role, epoch, node_id, auth) and a node that cannot read its own token never promotes; clients warn about a wide-mode token file and use host config, and `devfabric register` reports a rejected token instead of silently using the local registry; banner `control_auth` is `closed` for a tokenless gateway without `--allow-open`.

### Fix: fabric gateway control API fails closed and follows token rotation

- The gateway served `/register`, `/heartbeat`, `/close` and `/list` unauthenticated whenever no token was configured, and `HERDR_ENGINEERING_FABRIC_TOKEN` (documented) was read nowhere. The token now comes from that env var, host config `fabric.token`, or the token file (`HERDR_ENGINEERING_FABRIC_TOKEN_FILE`, default `~/.config/herdr-engineering/fabric-token`), for the gateway and the agent client alike.
- `herdr-eng devfabric gateway` refuses to start with no token unless `--allow-open` is passed (same convention as the ingest/relay `--allow-open`); a library gateway with no token rejects every control request. Tokens are compared in constant time.
- Rotation: precedence is env var, then the token file, then host config `fabric.token` (re-fetched every 300 s; a failed fetch fails closed instead of reading as "no token"). The gateway re-reads the file per request, so a rotated file needs no restart. The file must be a regular file (no symlink), mode 0600, UTF-8; anything else fails closed. Rotation does not revoke a leaked token for the 1 hour grace: restart the gateway to drop every superseded token at once. Replace the file atomically (write a temp file in the same directory, then rename it over `token`).
- Every superseded token stays valid for a grace of 1 hour (was a single previous token for 5 minutes), so a typo-then-fix or a torn read cannot evict the real previous token; a token seen once is never forgotten (a deleted file does not reopen the API). A missing file means "no token"; any other read error (for example a directory where the file should be) fails closed: control requests are rejected even with `--allow-open`, and the gateway refuses to start unless `--allow-open` is given.
- `FabricClient` resolves its token per request unless one is passed explicitly, so long-lived agents and CLIs follow a rotated file.
- `/healthz` needs no token: unauthenticated callers (the container healthcheck) get `ok`, `role`, `epoch`, `node_id` and `auth` only (no ports or leases); authenticated callers get the full payload. `FabricClient.healthz()` treats the minimal payload as "reachable but token not accepted" and raises, so the CLI probes and the HA peer check do not read a wrong token as healthy.
- HA pair: a peer that is reachable but rejects our token (token written on one node only) is no longer counted as dead, so the open standby no longer promotes over the tokened node; HA pauses with a warning until both nodes share the token. A node starting beside such a peer starts as standby.
- `GET /resolve-loopback/<port>` and `POST /rewrite` stay usable without a token (session tools only have the non-secret `HERDR_ENGINEERING_FABRIC_URL`); unauthenticated, `/resolve-loopback` omits the lease record and `/rewrite` takes at most 64 KiB. The startup banner reports `control_auth: "unreadable"` when the token file cannot be read.
- `scripts/fabric-stack.sh up` creates and checks the token directory the way `deploy.sh` does (real directory, owned by the caller, mode 0700) before compose runs; both refuse a wrong owner or mode.
- `deploy/control-plane/compose.yaml` mounts `~/.config/herdr-engineering/fabric-gateway` read-only at `/run/fabric` into `fabric-gateway` and `herdr-web` (both call the control API) and keeps `--allow-open` on the gateway for the rollout; `deploy.sh` creates the directory 0700. See the PR for the order.
### Fix: dashboard proxy is the same-origin gate for browser writes

- The proxy rewrote `Host` but forwarded the browser's `Origin`, so the control plane's same-origin guard answered 403 to every real browser write on Origin-guarded routes (attention, scheduler, ci, review-gate, artifacts, ...). It now refuses a mutation whose `Origin` is not its own `Host` (or a listed `public_hosts` name), or whose `Sec-Fetch-Site` is cross-site, or whose `Origin` is malformed, with 403 before attaching the operator key. When the `Host` is an IP literal, `localhost` or listed, the `Origin` goes upstream rewritten to the upstream's, so those writes work; for any other Host the `Origin` is forwarded unchanged as before, so a DNS-rebinding page reaches no route that was refused before (Origin-guarded routes stay 403 by an unlisted DNS name: list it in `public_hosts`). A cross-origin page can no longer make the proxy inject the operator key into upstream-unguarded allowlisted writes (`sessions/*/messages`, `assistants`, `library`, ...). Not closed without `public_hosts`: a DNS-rebinding page whose Host equals its Origin still reaches those unguarded routes, as on main; setting `public_hosts` closes it. Requests with no `Origin` (non-browser clients) are unchanged.
- New optional `control_plane.dashboard_proxy.public_hosts` / `public_scheme` (unset by default). With `public_hosts` (`host[:port]`; no port = any port, `:80`/`:443` equal the scheme default), any request whose `Host` is not an IP literal, localhost or listed is refused 403 (DNS rebinding), and an `Origin` naming a listed host is accepted although a front rewrote `Host` (list the front's own upstream name too). `public_scheme` requires the scheme of an `Origin` naming a listed host to match (also a listed IP); direct http access by an unlisted IP or localhost is unaffected. An `Origin` is the proxy's own when its name equals the `Host` name and its port equals the `Host` port (a `Host` without a port: any port for an IP or localhost, which keeps port-stripping fronts working, else the scheme default); for a listed name the port is the entry's, else the same-named `Host`'s, else the scheme default. A `public_hosts` entry that does not parse (e.g. `:99999`) is a config error. Invalid values are a config error (exit 2). Every refused write (allowlist, read-only, bad length, size, origin) drains up to 8 MiB of its body, for at most 2 s, so a sending client sees the answer and not a connection reset (a chunked body, 411, is not drained). A refused HEAD has no body.
### Fix: fabric agent no longer drops every lease on a failed listener scan

- `scan_once` ignored `ListenerInventory.complete`, so one timed-out or failed `ss`/`lsof` (an empty inventory) released every lease and stopped every forwarder, and the next cycle re-adopted them with new leases. A port is now treated as gone only on a complete scan.
- At startup, a remembered forward port that cannot be rebound (or whose forwarder fails to start) now drops the store entry and closes its lease, so the next scan re-adopts the service with a working forwarder instead of advertising a dead URL.

### Fix: web packages bug-hunt (collaboration sign-in/out, room polling, composer, member routes, nav badge)

- Collaboration: blocked site storage (`window.sessionStorage` throwing) no longer stops `collab-view.js`, and so the whole dashboard, from loading; it falls back to memory. Sign out now also ends the Cognito Hosted-UI session (`/logout?client_id=...&logout_uri=...`, `logout_uri` defaulting to `public_url`, which the user pool must list as an allowed sign-out URL). Every `CollabApi` call now times out after 30 s, so a half-open connection no longer freezes a room silently; `showRoom` removes its `focusout` listener on teardown instead of adding one per visit.
- Composer: a message delivered after the composer was disposed mid-send no longer survives as the stored draft.
- Dashboard: mission titles are refreshed after 60 s and a failed load is no longer cached as an empty map (`mission-titles.js`). The nav badge announces "N open, M critical" instead of "true critical".
- Member page: a slow library load no longer repaints over a newer route.
### Fix: required `unit-gates` no longer skips PRs that touch untested-by-list paths; Rust module gates fail in CI without cargo

- `scripts/ci/unit-gates-applicable.sh unit` is now default-run with a short exempt list instead of an allow-list, so `herdr_artifact_host/`, `ansible/`, `skills/`, `scripts/*.sh`, `package.json`, `package-lock.json` and any new top-level path run the suite (they previously passed the required check with no tests; `main-moved-check.sh` shared the gap). A test checks every tracked file against the matcher.
- `scripts/ci/module-gates.sh` no longer silently skips the scratch-retention and cargo-host gates when cargo is absent in CI: new `scripts/ci/require-cargo.sh` fails there unless `HERDR_ALLOW_NO_CARGO=1` is set explicitly.
### Fix: assistant store snapshot loading, ordering and locking

- `PostgresStoreBackend.load()` no longer turns a failed read into "no snapshot": it raises `StoreLoadError`, so a control plane that boots while Postgres is unreachable fails startup instead of running with an empty store whose first mutation overwrote the stored snapshot.
- Backend snapshot writes are numbered under the store lock and written one at a time; a slow older write can no longer land after a newer snapshot.
- The store's list/search methods, `request_tool_action`'s idempotency scan and `list_approvals` read the shared dicts under the store lock (no more `dictionary changed size during iteration`), and `decide_approval` is atomic, so two concurrent decisions on one approval cannot both pass the pending check.
### Fix: Slack approval of unseen text, calendar block-list alias, Rakazo first-bot cache

- Slack ask: the Approve buttons are only offered when the command is shown in full; a long (truncated) command gets Deny only and a "cannot be approved from Slack" note, and an approve tap on such an ask is refused (`not_fully_shown`).
- `CalendarPolicy`: blocking both `primary` and the primary's real id no longer blocks every other calendar.
- Rakazo bridge: the first-bot cache is keyed by `(tenant, user)`, so two spaces of one account no longer share a cached bot id.

### Feature: periodic `herdr-handoff reap --apply` timer (opt-in, off by default)

- `herdr-eng handoff-reap {run,status}` and `scripts/install-handoff-reap.sh` install a systemd user timer (Linux) or LaunchAgent (macOS) that closes panes replaced by confirmed handoffs, including those whose `confirm` was refused because the source pane was still working. Wired into `scripts/install.sh` (step 7f2) and, for Linux fleet hosts, `ansible/tasks/handoff-reap.yml` (`site.yml`, `fleet-sync.yml`).
- Policy is the `handoff_reap` section of `~/.config/herdr-engineering/config.yaml`: `enabled` (default `false`), `interval_minutes` (default 15, must divide 60), `legacy_roots` (per-project `.herdr/handoffs` directories imported into the shared store first). The unit files are always installed but the timer is only started where `enabled: true`; a malformed section fails closed to disabled. No environment variables.
- Covers the shared handoff store (`herdr-handoff` with no `--root`); `HERDR_HANDOFF_FILE`/`HERDR_HANDOFF_DIR` are removed from the job's environment so it cannot be redirected to one project's root. It never passes `--include-unrecorded` (tests pin this in the code, the units, the installer and the Ansible task).
- Nothing is enabled by this change: each host opts in separately.
### Fix: a dead Pi registration no longer blocks its host forever

- `fleet rollout`: `_live` reported "no reachable registration" and "two live registrations claiming one conversation" with the same untyped error (`session identity missing or ambiguous`), and `retire_identityless` deliberately refuses that untyped text because it cannot tell a stale husk from a real disagreement. A session whose instance is therefore gone was un-settle-able by design: every pass repeated the refusal, every later plan re-read the same registrations and reproduced the block, and the host stayed `blocked` with no supported way past it. The two cases are now distinct -- zero reachable rows raise the typed `MissingInstanceIdentity` refusal ("has no live instance to act on", `blockReason=identity_missing`), which the operator can settle with `fleet rollout retire-identityless`, while more than one live row keeps the untyped ambiguity refusal and is still never retired. Observed on the live fleet: 80 of 143 blocked session entries across 17 rollout records carried that one error, 60 of them with no `pid` at all, and the same registrations recurred in up to 6 of 17 plans.

### Feat: `templates/ci-merge-defaults`, the "CI does the merging" defaults as a repo-agnostic template

- `integrate-main.sh` (kept byte-identical to `scripts/ci/integrate-main.sh` by a test), Woodpecker `pr-gate.yml` and `main-gate.yml` snippets, and a README covering adoption plus the GitHub settings (`strict` off, required contexts, allow auto-merge, `gh pr merge --auto --squash`, `CHANGELOG.md merge=union`). Tested against real throwaway git repositories (`tests/test_ci_merge_defaults_template.py`).
### Agent rules: push work branches and open PRs autonomously

- `AGENTS.md` and the managed `SessionStart` reminder no longer require an explicit per-message ask before pushing herdr-engineering or opening a PR. Agents push their own work branches and open PRs themselves; pushing `main` and force-pushing or pushing someone else's branch stay forbidden (the reminder says so). The managed `PreToolUse` guard matches: a plain herdr-engineering push of a non-main branch is allowed, while force/destructive pushes (`-f`, `--force*`, `--mirror`, `--delete`/`-d`, `--prune`, `+` or `:branch` refspecs) still need the one-shot human token, including in the untokenizable-command fallback. Push to main/master stays denied.
### Fix: the control-plane inspector runs on the repo's clock

- `ControlPlaneRepo` now hands its own clock to `InspectorStore`. The inspector clamped event times to its 30-day
  retention ceiling around the wall clock, so with a fixed test clock (2026-10-24) four `tests/control_plane/test_llm_stats.py`
  transcript tests started failing on 2026-11-23 and turned unit-gates shard 3 red on every PR.

### Fix: Codex second-reviewer follow-ups from #820 (always-on-review.py)

- Codex spend is counted when Codex fails: a non-zero exit, time cap or missing answer after it reported tokens logs a
  `tier: codex` line with the priced `usage` (counts toward `daily_budget_usd`); with no tokens the line says
  `usage_unknown: true` and no cost is invented.
- A clean Opus approve is remembered per head in `state.json` (`opus_approved`, with the tier model) when only Codex
  failed to run, so the retry runs Codex alone; a new head, another model or `--force` reruns Opus. Cooldown and
  budget rules are unchanged; the entry is cleared once a verdict is posted.
- Config: `risky: null` / `risky.codex: null` mean the defaults; non-list or non-string `risky.globs` raise a config
  error naming the entry; a glob with a trailing `/` (`deploy/`) means `deploy/**`.
- A failed reviewer's `why` falls back to the first 300 characters of stdout (control characters stripped) when stderr is
  empty (claude prints its errors to stdout). A usage-limit message logs `why: usage-limit: ...`, is not a cooldown
  strike (state `limit_sha` lets the next poll retry), and stops the poll launching more reviews of that tier in the run.
- Review fixes: Codex has its own usage-limit bucket (a Codex limit skips only later risky PRs, before Opus; an Opus
  limit still stops Opus for the poll; a PR stuck on a limit is polled after the others). Codex limit detection scans
  only stderr and the `error` / `turn.failed` events, never command output. `opus_approved` also records the base sha
  (a retarget reruns Opus). A glob of only `**` / `**/` is refused.
- Rollback: revert this PR. The extra `state.json` keys (`opus_approved`, `limit_sha`) are ignored by older code;
  `risky: null` then fails the old config check, so restore a mapping (or remove the key) first.

### Fix: `fleet join` works from the installed copy (inventory import, verify step)

- The private-inventory helper moved into the package as `herdr_engineering/fleet_inventory.py`; `scripts/fleet_inventory.py` is now a thin shim that re-exports it (and still runs as a file for `scripts/fleet-join.sh`). The wheel ships only `herdr_engineering*`, so the installed venv could not import it, silently skipped the inventory, and the provision step then failed with "Could not match supplied host pattern". `herdr_engineering.fleet` no longer rewrites `sys.path` and the silent stub fallback is gone.
- `fleet join` step 4 (verify registration) read the leases through the native `herdr` binary, which has no `devfabric` command, so it always failed. It now runs `herdr-eng devfabric list --json` via this interpreter (`fleet.devfabric_leases`). `NativeHerdrAdapter.devfabric_list` and `devfabric_register` (same mistake, no other callers) are removed.
### Fix(ci): main push pipeline records green in the shared CI store (#931)

- `.woodpecker/unit-gates-main.yml` now exports `HERDR_CI_STATE` from the runner's `WPCI_CI_DIR` before `main-cron-gates.sh --record-green`, as `main-cron.yml` does. Without it every main push failed after a fully green suite with `HERDR_CI_STATE is not set`, and the cron never learned the commit was already green.
- `tests/test_ci_integrate_main.py` checks that every pipeline step calling `main-cron-gates.sh` exports the state dir first, and that the main push step passes it to `--record-green`.
### Chore(ci): drop the Woodpecker `opus-review` workflow; the always-on timer posts the status

- Removed `.woodpecker/opus-review.yaml`, `scripts/ci/opus-review.sh`, `tests/ci_review/test_per_pr_opus_review.py` and `docs/operations/per-pr-opus-review.md`.
- Why: the workflow needed an agent labelled `review-host=true` and `/usr/local/bin/herdr-opus-review`; neither ever existed, so `ci/woodpecker/pr/opus-review` stayed pending on every PR.
- The `herdr-always-on-review` systemd user timer is the only path and posts `Opus agent review`; docs and timer header updated to say so.

### Chore: bump herdr-handoff to 0.6.0 (c16c2dfd), `reap` required by the capability gate

- `lock/upstreams.yaml` pins herdr-handoff `c16c2dfdd86d6c0f22d8523f894718420e23d3a1` (v0.6.0; herdr-handoff#14, #16): `herdr-handoff reap [--apply] [--include-unrecorded]` (dry run by default) closes panes replaced by confirmed handoffs; `confirm` and `reap` check the live pane first and refuse with `source_is_caller`, `successor_gone`, `source_pane_reused` or `source_working`; takeover records `close_options.source_terminal_id`; successor panes are named `<project>: <goal summary>`.
- `herdr_engineering.handoff_dependency` now requires `reap` beside `takeover --direction`. 0.5.1 has every other command and flag but none of the live pane checks, and `reap` is the only marker its help gives for them. `herdr-eng doctor` on a host still on 0.5.x reports `does not provide reap (herdr-handoff 0.6.0 or newer is needed)`; fix with `scripts/install-handoff.sh`.
- `docs/operations/handoff.md` and the vendored handoff skill describe `reap`: dry run by default, `--apply` to close, and `--include-unrecorded` only after reviewing the dry run, because old records cannot detect pane-id reuse.
### Fix: Pi fleet rollouts converge - pinned target, resumable drain, bounded state (#844)

- **Pin the target**: `~/.config/herdr-engineering/pi-rollout-revision` (one 40-hex SHA; `HERDR_PI_ROLLOUT_REVISION` overrides) is now the canonical Pi runtime revision. `fleet rollout plan` falls back to it when `--pi-revision` is omitted and fails closed without one; an invalid pin is a failure, never a fallback.
- **Resume, don't re-mint**: new `fleet rollout latest` selector returns the newest unsettled plan (never `complete`/`abandoned`); the daily controller (`fleet_autoupdate`) now drains it after each successful controller update with a bounded pass window (`max_passes=3`) and mints a plan only when nothing unfinished exists **and** the pinned revision has never been rolled out - once a plan for the pin settles, every later night reports a skip instead of re-staging the converged fleet, and an abandoned plan for the pin is respected. A mismatched unfinished plan is reported, not resumed: settle it with the new `fleet rollout abandon --id ... [--reason ...]` (refused for complete rollouts) or finish it. Settled means settled: `apply`, `run` and `retire-identityless` now all refuse abandoned rollouts.
- **The nightly verdict stays the controller's**: the pi step's own failures - malformed pin file, unreadable store, rollout lock held by an operator command - are recorded as `pi_rollout.status: error` (or `prunedError` beside a completed drain) in the unattended run state; they never undo or replace a successful controller update.
- **Transient classification**: resume skips with `INTERACTIVE_BUSY` (a busy interactive session) now retry with a fresh operation instead of blocking the rollout forever; the Pi runtime's other not-yet conditions already report `busy` and were retried once a driver ran the passes. `bootstrap_required` sessions record a durable operator-facing `nextStep` (restart the stable Pi Host on that host so it loads the selected release).
- **Bound the state**: `fleet rollout prune [--older-than-days N]` (default 30, bounded 0-3650 - a negative horizon would delete every settled record) deletes settled records, judged by last pass or creation time; unsettled records are never touched. The daily driver prunes after each drain, and its pi-step result is recorded in the unattended run state so a skipped or blocked drain is visible without the journal.
### CI: unit-gates sharded into three parallel workflows, plus a mid-run main freshness check

- `.woodpecker/unit-gates-shard-{1,2,3}.yml` each run a third of the suite (`HERDR_UNIT_SHARD=<i>/3`) with the full PR pipeline (checkout, freshness, integrate-main, applicability, `unit-gates.sh`). `scripts/ci/unit-shard.py` splits test files by `sha256(path)`; `tests/test_ci_unit_shards.py` proves the shards partition the real suite (no test dropped or run twice) and that exactly one shard owns the slow tail (a shard without slow tests no longer runs an empty tail). The client (node) tests run in shard 1. `.woodpecker/unit-gates.yml` is now the aggregate: it `depends_on` the shards, so the required context `ci/woodpecker/pr/unit-gates` keeps its name, needs no branch-protection change and only goes green when every shard did, and goes red (not stuck `pending`) when one failed: it runs after the shards with `runs_on: [success, failure]` and fails closed on the shard states read from the Woodpecker API. Coverage is not enforced in this gate, so there is nothing to combine. `unit-gates-main.yml` is unchanged (one unsharded run per push to main). Rollback: restore `.woodpecker/unit-gates.yml` from the previous commit and delete the shard files.
- `scripts/ci/main-moved-check.sh` (last step of each shard): one `git fetch` after the tests; if main moved since integrate-main, a conflicting merge fails with the files named, a move in uncovered paths passes, and a move in covered paths with a clean merge passes with a WARNING (the push-to-main run proves the combination; `HERDR_MAIN_MOVED_POLICY=fail` makes it fail).
### CI: ui-gates covered-path list widened and locked by structural tests

- `ui-gates` already always reports (no `when: path`, `applicability` step, #719/#901 pattern), so the required `ci/woodpecker/pr/ui-gates` context cannot hang; what was left was drift. The `ui` list in `scripts/ci/unit-gates-applicable.sh` now also covers `herdr_artifact_host/member_static/`, `herdr_artifact_host/share_static/`, `schemas/`, the root `playwright.*` configs, `eslint.*.config.mjs` and `tsconfig.communications.json`, so a change to those runs the heavy Playwright steps instead of getting a skip-green.
- `tests/test_ci_gate_routing.py` asks the real script's matcher (no copy of the list) whether every tracked UI source, root browser-test config, helper script sourced by `ui-gates.sh` and file named by the `test:ui-gates` npm script is covered, so adding a UI tree without listing it fails the structural test. `docs/operations/ci-gates.md` states the rule.
### Per-target size cap and home-wide discovery

- New `build_cleanup.max_target_gb` (default 50, 0 turns it off) and `build_cleanup.size_cap_idle_hours` (default 2, minimum 1): a Cargo target over the cap that has been idle that long is cleaned by the 15-minute sweep, also below `high_water_percent`. The cap is enforced by the sweep alone: no pass runs after a build, and `build-coordinate` and the cargo wrapper are unchanged. It goes through the same checks as the pressure sweep (verified target, git-untracked, leases, per-target process scan, the lock, `cargo clean`); the sweep result names it `size_cap` in the new `reasons` map. Why: on 2026-11-20 beast's disk hit 100% with two checkouts holding 821 GB and 521 GB of stale `target/debug`; the old cleanup acted only at 85% and only under configured roots, and the service was never installed.
- Default roots are now `~`, `~/work`, `~/IdeaProjects` and `~/.cache` (each swept once, by its real path; the home walk skips roots listed separately). **This widens the earlier narrowed "explicit roots only" contract, at the user's explicit request** ("we have dozens of rust projects"). Every walk is bounded (depth 6, 500,000 directories) and never follows symlinks; on Linux it never stats into a mount point. Under the home directory it does not enter dot directories (`~/.cargo`, `~/.rustup`, `~/.local/share/Trash`) or `Library`; no root enters `.git`, `node_modules`, `.venv`, `venv`, `.cache` or `__pycache__`. A directory counts only when the existing verification accepts it (`Cargo.toml` beside it, Cargo's markers). A project outside any git repository, or whose only repository is `$HOME`, is still never cleaned. Explicit `roots` replace the default, and a root you name is searched as named.
- Huge targets (millions of entries) are capped: the size walk stops at the cap and idleness comes from Cargo's bookkeeping; an undecidable target is a warning. A target over the cap must pass the full foreign-content guard (owners, mounts, symlinks, special files, nested `Cargo.toml` or `.git`) over all of its content in a streaming walk before it is cleaned; one that cannot be walked in time is kept with a warning.
- Discovery and size checks run in a separate interpreter with a deadline per root (`HERDR_SURVEY_SECONDS`, 600): it holds none of the sweep's locks, is killed with its process group and reaped, and a cut-off survey keeps the rows it had. The 6 h target-list cache (`build-cleanup-targets.json`) is best effort and validated; the disk guard uses it and never walks `~`, and says when `~` was not searched. `max_target_gb` and `size_cap_idle_hours` are validated (`false`, `null`, text and negatives fall back to the default with one warning per process). `GIT_*` variables are ignored by the git checks.

- A busy check that times out keeps that one target with a warning; the sweep goes on with the others (before, it aborted the sweep). A target with no manifest beside it is skipped before any walk, and one in use is turned away by a scan that walks nothing, so neither is walked three or four times per sweep. A survey cut off by its deadline keeps what the child had written to the pipe. The depth limit reaches the survey child. A `max_target_gb` that rounds to zero bytes is the default with a warning. The docs now say that a target over the cap needs only `size_cap_idle_hours` whatever the pressure.

### Feat: per-PR `Opus agent review` through Woodpecker, as a template for any repo

- `.woodpecker/opus-review.yaml` (copy it unchanged into any repo): on `pull_request` and `pull_request_metadata` (the latter so a draft marked ready is reviewed) it runs on an agent labelled `review-host: "true"` (never the hive-heavy pool), `concurrency` limit 1, `skip_clone`, one step with the absolute command `/usr/local/bin/herdr-opus-review` and no Woodpecker secret (a missing secret fails every pipeline of a repo at compile time, so the status token is read by the wrapper from a host file, default `<passwd home>/.config/herdr-engineering/opus-review.token`, `--token-file` or `OPUS_REVIEW_TOKEN_FILE` to override; it must be owned by the running user and mode 0600, a missing, empty or loose file fails the step with exit 1; it is read into memory only). `scripts/ci/opus-review.sh` holds the logic: it does not use `$HOME` (the local backend isolates it; the real home comes from passwd, `--home`/`--config` override, `ISOLATED_HOME` documented), skips non-PR events, refuses a malformed `CI_REPO` or PR number, fails on an empty token, skips (exit 0) a fork head (`gh pr view --jq .isCrossRepository` prints `true`; any answer other than exactly `false` or `true` fails the step), then runs `always-on-review.py review --repo "$CI_REPO" --pr N --post --host-lock-wait`. Review exit 3 (head moved) and 4 (nothing to do) map to 0, which also masks no-review cases (draft, cooldown, budget, gates not green): the head status is the truth. 1 and 2 keep the pipeline red and the status pending. Guide, template and operator steps: `docs/operations/per-pr-opus-review.md`.
- Fork safety corrected: Woodpecker 3.20.1 runs an approved fork PR's own workflow on the review host, where it can read the token file and the logins. The docs no longer claim otherwise; the real control is the repo setting "require approval for all pull requests", and approving a fork PR is approving arbitrary code with the token. The review agent must be registered with the required label form `WOODPECKER_AGENT_LABELS=!review-host=true` so it takes only labelled workflows.
- `always-on-review.py`: a host-wide review slot (`state_dir/locks/host-review.lock`, `host_lock_wait_s`, `review --host-lock-wait`) is shared by the pipeline and the timer sweep, so one review runs at a time per host; a wait that runs out fails (exit 1) instead of being masked. A parent-death watchdog kills the reviewer's process group when the review process disappears (the reviewer runs in its own session and escapes Woodpecker's group kill), and each review removes stale worktrees and `refs/herdr-review/*` first. The pipeline timeout must be at least 150 minutes with defaults (the wait applies per tier). `review --tier all` runs tiers independently: a failing tier 1 no longer stops Opus. Managed-clone git calls clear inherited credential helpers before the `gh` helper and put `--` before the URL. The claude tier is `Read`, `Grep`, `Glob` only; the pi tier has `read,grep,find,ls` and needs a sandbox wrapper.
- Review follow-ups: documented pipeline timeout is at least 150 minutes (the host-slot wait applies per tier: 2 x 1800 + 900 + 3600 s; 720 minutes is fine); `approval_allowed_users` for trusted authors under require-approval; stale cleanup runs once per invocation, only for a run that will review, and `git worktree prune` only in managed clones; `auth=True` fetches (also into the operator's `clone:`) reset inherited credential helpers first; custom `XDG_CONFIG_HOME` and the exit-4-under-sweep case documented.
- Token file hardening: the file is opened once and checked and read through one descriptor, a path starting with `-` is refused, `GH_TOKEN`/`GITHUB_TOKEN` are registered for redaction at startup for every backend (a claude or codex finding that echoes the token is redacted), a failed git fetch names the real subcommand. Docs now say plainly that reviewers run as the agent's user and can read the token file (a prompt injection could exfiltrate it), that the agent should be user/org scoped, that Woodpecker masking no longer applies (no `set -x`), and that a sweep holding the PR lock leaves a newer head to the next hourly sweep.
- Follow-ups: `main()` also registers the operator's `gh auth token` for redaction (the hourly sweep runs without the wrapper, so claude/codex paths had no such registration; a missing or failing `gh` is ignored); the token-file swap test now really swaps the file after the check (a `stat` shim) and proves the token is read through the opened descriptor.
- `always-on-review.py` is repo-generic: the worktree now comes from `clones` (new, `[{repo, path}]`), else `clone` when its origin is that repo (a non-github origin is trusted as before), else a managed bare clone under `state_dir/clones/OWNER__NAME.git` from `clone_url` (new, default `https://github.com/{repo}.git`, with the `gh auth git-credential` helper; the PR fetch passes `GH_TOKEN`/`GITHUB_TOKEN` to git only). Before this, a `review` of another repo fetched `refs/pull/N/head` from the one configured clone and reviewed the wrong code or reported the head as moved. Rollback: remove the two keys; a config with only `clone` behaves as before for the repo that clone belongs to.
- `herdr-always-on-review.timer` is now an hourly backup sweep (`OnUnitActiveSec=1h`, was 5 min); it only catches a missed webhook. Operators who copied the old example keep their 5 minute timer until they recopy it. `docs/operations/independent-ai-review.md` no longer says a Claude session posts the status by hand: the review automation posts it.
### Disk hygiene on by default: pressure cleanup, one managed cargo target per worktree, disk-budget guard

- `build_cleanup` is now enabled by default with roots `~/IdeaProjects`, `~/work`, `~/.cache` (missing roots are skipped; the managed `targets/` dirs, any legacy slot pool and the `~/.cache/inferweave-native-admission/target` cache are covered). Opt out with `build_cleanup: {enabled: false}`. `scripts/install.sh` (step 7f, `--no-build-hygiene`), `fleet update` and `herdr-eng build-hygiene install` install the timer (systemd user unit on Linux, launchd interval job on macOS) and the cargo wrapper. The macOS sweep inspects processes with `lsof` (fail closed) because there is no `/proc`.
- Pressure mode: at usage >= `pressure_percent` (default 90) targets idle >= `pressure_min_age_hours` (default 2) are eligible; otherwise `min_age_hours` (24) applies. The hard 24h floor is now a 1h minimum. Live process, lease, open-fd and held-build-lock guards are unchanged.
- `fabric_build_cache` is enabled by default with `local.manage_targets: true`: each worktree gets one managed `CARGO_TARGET_DIR`. `./target` becomes a managed alias to it (an unmanaged `./target` is left alone) and `cargo clean` cleans that target. An explicit `CARGO_TARGET_DIR` is respected and `CARGO_INCREMENTAL=0` is the default for managed builds. An invalid `fabric_build_cache` section still fails closed to disabled.
- Disk-budget guard: when the build's filesystem is >= `refuse_builds_percent` (default 90) used and has < `refuse_builds_min_free_gib` (default 50) available, the cargo wrapper and `build-coordinate` (build commands only) run one rate-limited pressure cleanup, then refuse with exit 75 if the disk is still full. Disabled by `build_cleanup.enabled: false`. `HERDR_DISK_GUARD_OVERRIDE=1` builds anyway.
- `herdr-eng doctor` reports `disk_pressure`, `build_cleanup` and `cargo_host`; `herdr-eng build-hygiene status` prints them with the last cleanup run.
- Review round 17 (LOW cleanup): a path containing `:` or `=` on a command line is kept whole as well as split; the `=` split is linear; the name lookup walks the target once for all names; a rewritten process title (`sshd-session: user@pts/3`) is not taken as a name; the docs state the 15-character `comm` prefix match and the name rule; tests over a real `/proc`-shaped directory separate the `comm` match from the argv[0] basename.
- Review round 16: the command-line fallback for this user's non-dumpable processes also handles a program started by a relative path (`./target/release/server`): its basename, and the process `comm`, keep the target when a regular file of that name is in it (errs toward keeping); absolute paths are also read after every `=`, in `:` lists and in glued short options (`-L/x`); the docs and docstring list exactly what is matched. Tests: a real non-dumpable binary run by a relative path, the documented limit (cwd in the target, nothing on the command line, is not seen), a busy-check child that dies without an answer, and a real lock wait instead of a sleep in the no-owner test.
- Review round 15: this user's non-dumpable processes (root-owned `fd/`, e.g. `ping`, file-capability binaries, `prctl`) are no longer skipped outright: their command line (program path or absolute argument inside the target or workspace) still marks the target busy, and they are detected only that way; the round-14 wording is corrected (a root-run process such as `sudo -i` is caught by the root-owned `/proc/<pid>`, this user's non-dumpable one by the `fd/` owner; only the 0500 entries turn root-owned, not `/proc/<pid>`); a timed-out busy check is proven to kill its grandchildren too, and the group kill only runs while the child is unreaped; a markerless clean with no ownership record is skipped with a reason instead of writing a `null` marker. Follow-ups: CI wiring for `tests/test_build_cleanup*.py`, and re-running `_inspect` after the scan.
- Review round 14: a process whose `/proc` entry belongs to another uid and refuses `fd/` is skipped whatever its ancestry, so non-dumpable processes no longer abort every sweep; the docs now say plainly that without root only this user's processes and rootless-container processes are visible (root services and rootful containers under another uid are not detected, `hidepid` or not); the busy-check child runs from `/` with `-P` (no imports from the sweeper's directory), ignores the sweeper itself, and its whole process group is killed on timeout or any error; a non-finite or non-positive `HERDR_BUSY_CHECK_SECONDS` falls back to the default with a warning; a leased target is skipped before the scan (the lease is read again after it); namespace tests cover the `..` walk, an fd, a mapping and the executable.
- Review round 13: the Linux busy check takes identity from the kernel (stat of the `/proc` cwd, exe and fd links, `maps` dev:inode), so a process in another mount namespace is seen; the scan runs in a child with a deadline (`HERDR_BUSY_CHECK_SECONDS`) and fails closed with "busy check timed out" instead of hanging on a dead mount; a managed target whose marker a killed `cargo clean` removed is cleaned from its owner record; processes of every uid are scanned where `/proc` is readable (not detected where it is not: `hidepid`, macOS without root); the lease is read after the process scan; the lease race tests sync on the sweep's real busy-check child instead of a fake `git` on PATH.
- Review round 12: busy targets are matched by (device, inode) of the process path and its ancestors, not by string (a root spelled `work` for `Work`, symlinks and bind mounts now match); one undeletable `.trash-*` leftover is reported in `skipped` and no longer stops the root; ownership is recorded in `targets/.owners/<name>.json` so a `cargo clean` killed mid-way cannot orphan a managed target without its marker; `cargo clean` pins `build.build-dir` to the target and drops `CARGO_BUILD_BUILD_DIR`, so a shared build-dir survives; an orphan gets the same busy scan as an idle target; tests no longer stub `_process_paths`, `measure`, `_usage` or `_untracked`.
- Review round 11 redesign: the idle-target busy check is per target (a process using the target or its workspace, or a cargo naming it via `CARGO_TARGET_DIR`/`--target-dir`), not a host-wide cargo census, so a rust-analyzer elsewhere no longer blocks cleanup. The scan is taken fresh before each clean and fails closed; the `processes=` test seam is gone and the refusal message names the blocker. An orphan needs a not-found workspace with a listable parent (EACCES is not an orphan) and is renamed to `.trash-*` before deletion (leftovers are removed later). `prepare_target` writes the standard `CACHEDIR.TAG` (and backfills older targets); the marker, directory and tag are restored in a `finally` even when `cargo clean` times out.
- Review round 2: each shared-pool slot belongs to one worktree (the slot name carries a hash of the worktree path), so two worktrees never share final artifacts, and `cargo clean` only cleans the caller's slots. The guard and the sweep use the same df-style usage figure. The watcher recomputes its roots every cycle (pools created after start are swept) and includes the pools and `targets` dirs of enrolled storage tiers. Old per-workspace `targets/<label>-<id>` dirs (proved by their markers) are swept when idle. A cache-only target inside a git repo is never removed when tracked. Roots are no longer realpath'd, so the canonical-root guard works. `build-coordinate` guards build commands only (not cargo, which the wrapper guards even under build-coordinate, and not `go test`/`go vet`). `build-hygiene install` exits nonzero on a failed step, skips the unit when there is no systemd user bus, honours `XDG_CONFIG_HOME`, and `fleet update` no longer installs the cleanup unit twice (`--skip-cleanup-job`). launchd reloads wait for the bootout to finish, and doctor warns when the job is written but not loaded. `target_mode` is the `TargetMode` enum. Toolchain identity is cached, and the sweep scans processes once per run (rescanning only right before a removal).
- Narrowed scope (replaces the race-fix rounds 3 to 9, which did not converge): the default-on job deletes only two things. (1) A managed target whose owning worktree path no longer exists (orphaned; decided again under the manager lock, and only when the directory that held the worktree still exists, so an unmounted volume proves nothing), removed with a plain `rmtree` at any age. (2) An idle target (24 h, or `pressure_min_age_hours` under pressure) whose workspace exists, and only while no cargo-family process runs for this user at all: `cargo`, `rustc`, `rustdoc`, `rust-analyzer`, `cargo-*`, or any process whose executable, working directory or open file lies inside a target directory (test binaries included). One process snapshot is taken before deciding; one such process skips every non-orphaned target for the pass, and an unreadable process list fails closed (`aborted`). The delete is `cargo clean --manifest-path <workspace>/Cargo.toml --target-dir <dir>`, so Cargo does its own locking; a managed target is cleaned under the manager lock and its directory and marker are put back, so `./target` never dangles. Pressure cleanup (the guard and the reserve) uses the same safe set with the pressure idle floor and still refuses with exit 75 when nothing safe can be freed. Cache-only pool targets (no workspace to clean through) are no longer swept. Removed: tombstones and their claims, moving profile contents, the sweep's flocks on Cargo lock files, the snapshot-reuse window, the per-candidate process rescans and `_SLOW_SCAN_SECONDS`. Kept: the zombie-safe `/proc` scan, the single disk measurement, the repo-local config allowlist, the lease checks (read last, just before the clean), the 2-minute back-off after a failed sweep. `run_once`, `relieve` and `check_build_allowed` take an optional `processes` callable returning the snapshot (the tests' only seam).

### Fix: unit-gates retries only the failed tests once, has a flaky quarantine, and four load/disk-sensitive tests are deterministic

- `scripts/ci/pytest-rerun.sh`: after a failing fast pass, the test files of the failed/errored tests are rerun once, serially and whole (same-module pollution still reproduces). A test failing again, skipped on retry or newly failing is a real failure and still fails the gate; a failed test that passes on retry is logged as `RETRIED: <id> ... first failure: <summary>` (also as `RETRIED-JSONL` log lines) and listed in `pytest-retried.jsonl`. A test needing a retry in 3 runs within 14 days is flagged as a `CHRONIC FLAKE` (loud warning, `chronic_runs` in the JSONL row; only on push/cron events, updated under flock; fails only with `HERDR_PYTEST_CHRONIC_FAIL=1`; counter under `HERDR_CI_STATE`, off with a log note if unwritable). Session-level failures (coverage floor, `INTERNALERROR`) are not retried. `HERDR_PYTEST_RETRY=0` disables it, `HERDR_PYTEST_RETRY_MAX` (numeric, default 25) caps it.
- New `@pytest.mark.quarantine_flaky(reason, issue, expires)` marker and `tests/flaky-quarantine.toml` (`scripts/ci/herdr_flaky_plugin.py`): a quarantined test still runs and is reported but cannot fail the gate; reason, issue and an expiry at most 90 days away are mandatory, the marker works on a test function only (module/class level is rejected), enforced by `tests/test_ci_flaky_quarantine.py`, and an expired entry stops quarantining.
- Tests: the huge-blob starvation test runs a time-bounded flood (until the timing is over, in-flight sockets capped) and compares against the median idle latency with a 2.5 s floor instead of a fixed 1.0 s (it failed at 1.09 s and exhausted file descriptors); it was verified to fail a server that hashes big blobs without the gate; `test_liveness_window_and_clamp` no longer depends on approval and revocation being more than 1 ms apart; `test_build_chaos` pins `min_free_disk_percent: 0` so it does not depend on the free space of the host (3 tests failed on a 99%-full disk).

### CI: PR runs test PR + main, and main gets its own full run

- New `scripts/ci/integrate-main.sh`, run after `checkout` in `unit-gates`, `ui-gates`, `artifact-handoff` and `structural-gates`: on a `pull_request` event it merges current `origin/main` into the PR head (throwaway identity, conflict aborts and names the files, other events no-op), so a green PR run means "PR + main" once branch protection `strict` is off. `ui-gates` and `artifact-handoff` now fetch full history for the merge; the ui-gates stale-base notice stays silent when main is already merged.
- New `.woodpecker/unit-gates-main.yml`: the full unit gates on every push to main (`hive-heavy`, concurrency group `unit-gates-main`). `scripts/ci-pipeline-freshness.sh` marks a superseded main run (`.ci-main-superseded`, exit 0, so no false "CI failed - main"); its unit-gates step then skips and never records green. A superseded PR run still exits 1; cron behaviour is unchanged. `integrate-main.sh` names the real target branch in its conflict message, and the new pipeline files plus the freshness and integrate scripts are covered paths for unit-gates applicability. A green run records its commit via `main-cron-gates.sh --record-green` so the cron skips it.
- `.gitattributes`: `CHANGELOG.md merge=union` (local merges only, not GitHub's merge button). See "Merge gating" in `docs/ci-and-reviews.md`.

### Fix: CI report relay skips bundles whose provider run was removed

- A bundle whose Woodpecker run the control plane reports as removed (`This Woodpecker run was not found; it was removed or pruned.`, which the provider raises as `run_not_found` only when the pipeline GET returns 404 and the repository GET still succeeds) is a permanent skip: it is never published, is recorded in the checkpoint as `state: skipped` (`reason`, `skipped_at`), logs `CI report relay: skipped <bundle>: provider run removed`, and is not re-read on later passes. The summary line is now `N published, M failed, K skipped` and the exit status is 1 only for failures. A 404 that is repository-level, auth-level, endpoint-level or unconfirmed (deleted or re-created repository, lost token access, wrong base URL, proxy 404, repository lookup miss) keeps the generic `not_found` answer and stays a retriable failure. A run that exists with a mismatched identity, HTTP errors, timeouts, auth errors and malformed responses still fail closed and stay retriable.

### Perf: unit gate no longer collapses to one worker on busy shared nodes; slow tail runs alongside the fast pass

- `scripts/ci/pytest-parallel.sh`: the load-average bound introduced in #888 (`workers = cpus - int(loadavg)`) gave `-n 1` on shared hive nodes where other tenants' load was 28 on 16 CPUs (19 of 101 measured runs took 104-120 minutes against 15-22 with 8 workers). Load now only shaves workers down to a floor of half the CPUs, at least 4 (never above the CPUs); the cgroup CPU-quota cap (#830) is unchanged and still limits workers for genuinely small budgets. The default `--maxprocesses` stays 8 until a 12-worker run is measured (an empty `HERDR_PYTEST_MAXPROCESSES` means the default); `HERDR_PYTEST_WORKERS` / `HERDR_PYTEST_MAXPROCESSES` still override. Conscious trade-off: on a 4-CPU node the floor equals the CPU count, so load is ignored there (the #888 rerun-once on xdist crash and a cgroup quota remain the protection).
- `scripts/ci/unit-gates.sh`: the serial `slow` tail runs concurrently with the fast xdist pass (one niced process (`ionice -c3` where available), `[slow]`-prefixed output, own watchdog, own `mkdtemp` dirs and database socket (TMPDIR itself is shared), no shared pytest cache), failing the gate if either pass fails; `HERDR_PYTEST_SLOW_CONCURRENT=0` restores the serial order. Both passes print `--durations=40` and a per-test-file / per-worker time summary (`scripts/ci/pytest-plugins/pytest_file_times.py`) to measure `loadfile` imbalance.
- `.woodpecker/main-cron.yml`: `labels: {cluster: hive-heavy}` (cron runs had landed on 4-CPU nodes) and a `concurrency` limit of 1 so cron runs never overlap.

### Chore: pin herdr-handoff 0.5.1

- `lock/upstreams.yaml` pins herdr-handoff to `a035dcab040b3288c10f15abfdb52620f4bc37ac` (herdr-handoff#13, v0.5.1). `herdr-handoff confirm` now closes the source pane when `takeover` gave up waiting (its `--confirm-timeout`) and the successor confirms afterwards, through the same close gate as takeover (unpushed commits, a dirty worktree or another tab refuse). It never closes after `--keep-source`, for a 0.5.0-written record, with no source pane recorded, or while a takeover holds the lock. Its JSON gains `close` (`source_closed`, `already_closed`, `refused`, `not_attempted`), `source_closed`, `reason` and `source_closed_at`, and it exits 0 whenever the confirmation is recorded. 0.5.1 adds no CLI flag, so `check_handoff_dependency` is unchanged (it still names 0.5.0 as the minimum). The handoff skill and operations docs now describe a late `confirm` closing the source.
### Handoff autopilot stands down for Herdr-managed sessions

- The autopilot (#884) no longer fires in a pane the controller manages: `HERDR_PANE_ID` set and non-empty, a controller running (a linked plugin alone does not count), and the controller not switched off for that pane; it is asked only when the autopilot would otherwise trigger. It answers code `herdr_controller_managed` (`trigger: false`) with the reason; `handoff autopilot status` shows it under `herdr_controller`, the Claude hook (`hook --record`) notes it in the ledger once per session as `skipped` (Pi sessions show it in `status` and `check --json`), and `doctor` keeps `handoff-autopilot` `ok`. Everywhere else it behaves as before, and any error while deciding fails open towards the autopilot, so every session has exactly one policy. A `skipped` ledger entry is not a trigger: `uptake`, the uptake stamp and `doctor` count only sessions that were told. The autopilot no longer reads the retired `context-hook` marker. See `docs/automatic-handoffs.md`.

### Chore: pin herdr-handoff 0.5.0

- `lock/upstreams.yaml` pins herdr-handoff to `aed3e8274e6948a774682a55d99b7cd9ef113914` (herdr-handoff#12, v0.5.0: shared handoff store, one takeover per handoff id, `takeover --direction`, `--relaunch`). `check_handoff_dependency` now also requires that `herdr-handoff takeover --help` lists `--direction` (reported as `takeover --direction`, naming 0.5.0), so `herdr-eng doctor` reports an installed 0.4.0 as too old until `scripts/install-handoff.sh` is run.

### Security: repo-local config can no longer set `handoff_autopilot`

- `load_config` and `declared_keys` now ignore `handoff_autopilot` in the repo-local `./config/herdr-engineering.yaml` and `./herdr-engineering.yaml` (the repository being built is untrusted), exactly as `fabric_build_cache` already was, because neither is in the `REPO_LOCAL_ALLOWED` allowlist (a repository's own config may only set the keys listed there). Set it in `~/.config/herdr-engineering/config.yaml` or `$HERDR_ENGINEERING_CONFIG`.

### Fix: automatic handoff reads the context window from the session's model

- `handoff autopilot` no longer hard-codes a 200k window for Claude Code. The transcript names the model, and `handoff_autopilot.model_context_windows` (YAML; a model-name substring to its window, longest match wins, merged with the built-in `[1m]` -> 1,000,000) gives its window; the Stop hook, `check --transcript` and `record --transcript` all apply it and report `window.source: "model"`. The harness's own stated window still wins, then the model, then a declared `context_window_tokens`, and only then the 200k assumption (with escalation past it). New `transcript_model()` reads `message.model`.

### Fix: unit-gates reruns the parallel pass once if xdist workers die

- `scripts/ci/pytest-rerun.sh`: exit 3 or a worker-crash signature reruns the parallel pass once on 2 workers (never for test failures or hangs); `auto` workers also shrink on a loaded host.

### Fix: `fleet status` and `doctor` no longer depend on the working directory

- `fleet.repo_root()` no longer falls back to the cwd. It uses `HERDR_ENGINEERING_REPO`, else walks up for `ansible/inventory/hosts.yml` (or a checkout's `ansible/playbooks/fleet-status.yml`), else raises `FleetError` naming the paths searched. `fleet status` reports a missing inventory as `no inventory at <path>` on stderr and exits 2 (setup problem), distinct from exit 1 (ansible ran and hosts failed); `--json` carries `error` and `error_kind`. `fleet join`/`update` and `stage_pi_runtime` return that error in their report instead of running ansible against a nonexistent path.
- `doctor`: the `lock` and `plugins` checks locate `lock/upstreams.yaml` from `HERDR_ENGINEERING_LOCK`, then `HERDR_ENGINEERING_REPO`, then the checkout the package was imported from, never the cwd. With no checkout (an installed copy) both report `unknown` ("not applicable: no checkout found") instead of a warning.
- `doctor`: the `tailscale` check reads `tailscale status --json` and treats `BackendState == "Running"` as up (exit code when the output is not that JSON); the plain-text check looked for the word "tailscale", which that output never contains, so it always warned. The decision is the pure function `tailscale_running`.
- `fleet rollout plan` and `fleet recover plan`/`run` outside a checkout print that error as JSON and exit 1 instead of a traceback.
- `scripts/herdr-gateway-monitor.timer` triggers on `OnCalendar=*:0/5` instead of `OnBootSec=`/`OnUnitActiveSec=`. With `Persistent=true` the old unit stopped firing after a reboot whenever the user manager started later than the boot delay. Installed copies need the unit re-copied and `systemctl --user daemon-reload`.

### Fix: CI report relay failures are no longer silent

- `ci_report_relay` printed only `N published, M failed`. It now writes one stderr line per failing bundle (`CI report relay: failed <bundle>: <reason>`), per unreadable source file (errno name), and for whole-run failures, plus the reader exit status on source errors. Reasons are fixed strings or errno names: no URLs, keys or bundle contents. Delivery and fail-closed identity checks are unchanged.

### Workflow hooks: hand off automatically on long context
- The `PreCompact` message and the `SessionStart` reminder now tell the agent to run `/handoff` itself, without asking the user, after background subagents have reported and work is committed/pushed where allowed (previously a suggestion). The reminder also says not to ask the user about choices the defaults already settle. The hooks still only print text.

### Fix: workflow guard denies guarded pushes, with a one-shot human override
- `ask` is not prompted in Claude Code auto permission mode, so the herdr-engineering push rule (and the unknown-cwd push rule) did nothing. `herdr_engineering/workflow_guard.py` now returns `deny` for both; the reason tells the agent to ask the user to run `! touch ~/.local/state/herdr-engineering/allow-push` and retry. If that token file exists, the next guarded push is allowed and the file is deleted (one-shot; `HERDR_WORKFLOW_ALLOW_PUSH` overrides the path). Main/master push denial is unchanged and never consumes the token; the guard still fails open on internal error.
- `Write`/`Edit`/`MultiEdit`/`NotebookEdit` on the token path, and `Bash` commands mentioning it, are denied so the agent cannot approve itself. A speed bump, not a security boundary (substring check). See `docs/operations/workflow-hooks.md`.

### Fix: ui-gates reports on every pull request

- `.woodpecker/ui-gates.yml` drops its `path:` filter for an `applicability` step (`scripts/ci/unit-gates-applicable.sh ui`), so the `ui-gates` context can be required without blocking PRs that touch no UI path. The decision reads `CI_PIPELINE_EVENT` (what Woodpecker exports) with `CI_EVENT` as fallback, and the `ui` case also covers `pyproject.toml`, `scripts/ci/journal.sh` and `scripts/ci/cache-trust.sh`, which `ui-gates.sh` depends on.

### Automatic handoff controller

- New `herdr_engineering/handoff_controller/` and `herdr-eng handoff controller run|status`: one process per host reads every Herdr-managed Claude, Codex, Pi and OpenCode session every 5 seconds. At 80 percent of the context window (hard ceiling 12 points higher) it types the drain order into an idle session, waits up to 5 minutes for the briefing (a headless fallback writer produces one when the session does not; marked `provenance.fallback`; not enabled for Codex or OpenCode, whose CLIs cannot turn every tool off), commits tracked changes to the feature branch, and runs `herdr-handoff takeover` to start a successor below and close the source pane. It never closes a pane itself, never commits on main or master, and never pushes unless the repository is in `HERDR_HANDOFF_PUSH_REPOS`. If a takeover fails three times it compacts the session with a pointer to the briefing (last resort).
- Events go to `events.jsonl` in the shared store: twelve `handoff.*` events, colours green (handed off), orange (failed, compacted) and red (stuck, with a reason). Per-session switch `herdr-eng handoff auto off|on`; `HERDR_HANDOFF_AUTO=0` for all. After three non-green chains a session is left alone (`handoff.skipped`, `gave_up`). One controller per host (`flock`).
- One shared handoff store (`HERDR_HANDOFF_STORE`, default `~/.local/share/herdr-handoff/store`); `scripts/handoff_import_legacy.py` copies the old per-repo stores into it. Requires herdr-handoff 0.5.0 (unreleased). Plugin manifest in `deploy/herdr-handoff-controller-plugin/`; `scripts/install.sh` does not link it yet.
- New `herdr_engineering/handoff_controller/` and `herdr-eng handoff controller run|status`: one process per host reads every Herdr-managed Claude, Codex, Pi and OpenCode session every 5 seconds. At 80 percent of the context window (hard ceiling 12 points higher) it types the drain order into an idle session, waits up to 5 minutes for the briefing (a headless fallback writer produces one when the session does not; marked `provenance.fallback`; not enabled for OpenCode), commits tracked changes to the feature branch, and runs `herdr-handoff takeover` to start a successor below and close the source pane. It never closes a pane itself, never commits on main or master, and never pushes unless the repository is in `HERDR_HANDOFF_PUSH_REPOS`. If a takeover fails three times it compacts the session with a pointer to the briefing (last resort).
- Events go to `events.jsonl` in the shared store: eleven `handoff.*` events, colours green (handed off), orange (failed, compacted) and red (stuck, with a reason). Per-session switch `herdr-eng handoff auto off|on`; `HERDR_HANDOFF_AUTO=0` for all. After three non-green chains a session is left alone (`handoff.skipped`, `gave_up`). One controller per host (`flock`).
- One shared handoff store (`HERDR_HANDOFF_STORE`, default `~/.local/share/herdr-handoff/store`); `scripts/handoff_import_legacy.py` copies the old per-repo stores into it. Requires herdr-handoff 0.5.0 (unreleased). Plugin manifest in `deploy/herdr-handoff-controller-plugin/`; `scripts/install.sh` links it and the session pane plugin (`herdr-eng handoff controller link`, idempotent; the manifests name `~/.local/bin/herdr-eng` when it links to the checkout's `.venv`, else that binary with a warning; `--no-handoff-plugins` or `HERDR_INSTALL_HANDOFF_PLUGINS=0` opts out), and `herdr-eng handoff auto check` and `herdr-eng doctor` report a host with no controller linked or running, or linked but disabled. `handoff auto uninstall` does not unlink them (`herdr plugin unlink <id>`). A symlinked `~/.claude/settings.json` (or other file written by the shared installer helpers) is now written through to its target and stays a link; a read-only file is still refused only by the two hook-migration installers.
- The old per-harness auto-handoff policies are retired in the same release (see "Retired" below); they must not run alongside the controller.
- Not yet verified by a live run (`tests/e2e/test_handoff_controller_live.py`, opt in with `HERDR_HANDOFF_LIVE_E2E=1`): the headless flags per harness and the Herdr agent names of Codex and OpenCode. See `docs/operations/handoff.md`.

### Retired: the `/handoff` reminder text and the 50% context hook

- `herdr-eng handoff context-hook` and `herdr_engineering/handoff_context_hook.py` are removed (see below); `herdr-eng workflow install` no longer registers a `PreCompact` hook and the managed `SessionStart` reminder no longer mentions `/handoff` (`EVENTS = ("SessionStart", "PreToolUse")`). The controller decides when a session hands off.
- `herdr-eng workflow install|repair` removes a previously managed `PreCompact` entry (marker `herdr-workflow:managed` and `workflow hook PreCompact`); a foreign `PreCompact` hook, even in the same entry, is kept. A second install is a no-op and writes no backup. `workflow doctor` reports a leftover one as drift. A session that still has it keeps working: the old command prints nothing and exits 0.
- autospec's context monitor skips sessions started from a Herdr pane (`AUTOSPEC_CONTEXT_MONITOR_SKIP_HERDR`, default on, keyed on `HERDR_PANE_ID`); a pane with the controller switched off therefore has no automatic policy. Upgrade once with `herdr-eng handoff auto install` and `herdr-eng workflow install` (see `docs/operations/handoff-auto-triggers.md`); until then the old hook commands are a silent no-op. The `/handoff` skill now requires herdr-handoff 0.5.0.
- `herdr-eng handoff auto install|uninstall` removes the retired `UserPromptSubmit` `context-hook` entry (marker `herdr-handoff-auto` and `handoff context-hook`), with a backup under `~/.local/share/herdr-engineering/handoff/backups/`; `handoff auto check` reports it as `stale`. `HERDR_HANDOFF_AUTO=0` is now the controller's global off switch.

### Session side pane

- New `herdr_engineering/side_pane/` and `herdr-eng side-pane run|ensure`: a read-only viewer to the right of the sessions of a Herdr tab, one per tab, created by the handoff controller with the first managed session. Three tabs (Summary, Handoffs, CI; keys `1` `2` `3` `Tab`, `q` closes). It reads the controller's event log, the shared handoff store, Herdr, `gh pr list` with your own gh login and Woodpecker's public reads (no Woodpecker token).
- Focus and dismissal: opening the pane acts on focus only when the new pane holds it and restores it with one directional focus call (else a typed `left_on_side_pane` outcome); `q` is ignored for the first 2 seconds; a pane closed any way other than `ensure` counts as dismissed for that tab until a new managed session appears. `herdr-eng side-pane ensure --tab TAB_ID` reopens it; the plugin `deploy/herdr-session-pane-plugin/` adds an "Open session side pane" action (linked by `scripts/install.sh`).
- `side-pane run --snapshot --tab-view {summary,handoffs,ci} --width N` renders one frame to stdout without Herdr; `tests/test_side_pane_snapshot.py` runs it in CI against a real event log. Live check: `tests/e2e/test_side_pane_live.py` (opt in with `HERDR_HANDOFF_LIVE_E2E=1`, spends model tokens, not run in CI).
- CI tab for the whole chain: one card per branch the chain's records name in the session's repository, from one `gh pr list --json ...statusCheckRollup` per refresh. Every rollup check is a chip (GitHub Actions, GitGuardian, review bots, Woodpecker contexts, deduplicated against the public per-workflow chips); a check that never started is grey, a required check that never reported is not invented, and a failing card names its first failing check. "Pushed by #N, 2h ago" comes from the chain's `handoff.wip_committed` events and is left out when unknown.
- Honest commit label and stricter CI cards: a card line reads "committed by #N, 2h ago" (with ", not pushed" when the checkpoint's push failed or was not allowed) only for events that carry a commit; the log records the controller's checkpoint commit, not pushes the session made itself. A card is ready only when every known check passed: cancelled, skipped or unknown states (shown as a grey chip with the state named) need attention, queued ones are running. A green chain without a record after a red one clears the red live state; `Totals.sessions` counts sessions, not failed attempts; reasons keep a short detail; `side-pane run` refuses (exit 2) when `herdr agent list` does not answer.
- Bounded creation: the controller's call into `ensure` takes at most 5 seconds and never waits on the tab lock (no unbounded fallback call); a split that outlives the budget is closed with 2 seconds of its own, and an unclosable pane keeps its marker; unavailable, locked and missing-runner outcomes are retried by the controller's three-failure logic, while too-narrow and dismissed are final. The viewer is started with the controller's state, store and HOME, and polls less (agent list per refresh, pane list every 10 seconds, no layout read for one session, slower after a quiet minute).

### Fix: run-watched review nits from #824

- `scripts/ci/run-watched.py`: `Out.write` also survives a full non-blocking stdout (EAGAIN), a closed fd and a `None` stdout (it stops forwarding and keeps draining); the process group is only signalled once the root is reaped if a live process still holds that group (a reaped pid can be reused); `has_token` no longer caches negatives (a fork child reads as its parent until it execs); CPU history is keyed by (pid, start time) so a reused pid cannot corrupt deltas; signal handlers are installed before the command is spawned; bad SOFT/HARD, a missing `--` or command print a usage error and exit 2, and an unstartable command exits 127/126 with a message instead of a traceback; a hang now stops the tree with SIGTERM, a short grace, then SIGKILL (exit still 124).
- Output forwarding waits for a slow reader instead of dropping the rest of the log (stdout is made blocking at startup, EAGAIN retries once writable); only EPIPE/EBADF/closed stdout stop forwarding, with one stderr notice (a reader that stops reading without closing still blocks the writer; that predates this change). The pipe is drained during the kill grace (bounded), so SIGTERM-handler cleanup output is kept. `HERDR_WATCHED_ORPHAN_GRACE` is validated before the spawn.
- Tests: a `pidfiles` fixture kills leaked daemons and removes pidfiles on teardown.

### Memory client pin: 0.1.8 → 0.1.10

- `PINNED_REVISION`/`PINNED_CLIENT_VERSION` and `lock/upstreams.yaml` move to `eb1338a4067d4e17910a9f651fc7c08551f5b8a9` (client `0.1.10`, contract v1 unchanged). Three commits ahead of the previous pin: `f3a20ba` makes recall/documents degrade to empty on a never-written bank and adds `provision` plus per-bank `bank:<id>` doctor checks (herdr-memory#18, closes #17); `66eb463` bumps to 0.1.9 and adds the repo's first CI plus the version-parity test that guards the two-builds-one-version-string incident (herdr-memory#19); `eb1338a` gives every queued operation a terminal attempt record and surfaces vanished rows as `status.lost` / doctor `lost` (herdr-memory#20, closes #4). Every host in the fleet was already running 0.1.10, so `memory doctor` reported `package_revision` and `client_version` drift on each one; this reconciles the pin with what is deployed rather than pulling anything new in, the same shape as the 0.1.7 → 0.1.8 bump. Verified against the upstream tree at that revision (`pyproject.toml` → `version = "0.1.10"`, `herdr_memory/__init__.py` → `__version__ = '0.1.10'`), not from a tag or a version string in a log. `tests/test_memory_install.py::test_pin_matches_lock` fails on a partial bump. Because the pin is part of the managed-block marker, hosts need `herdr-eng memory upgrade` after this ships.

### Fix: fleet watcher review follow-ups (orphaned children, lock edge cases), on top of the self-restart

- SIGTERM and the hard-limit exit 70 now stop an in-flight local `host-apply`: the watcher sends it SIGTERM first (5 s grace), host-apply kills the commands it started (they run in their own sessions, which a group kill of host-apply would miss), then SIGKILL. A grandchild that deliberately leaves its session is still left to the systemd cgroup (`KillMode` default `control-group`), which kills the whole unit on stop. The hard-limit exit never releases the registry lock before `os._exit(70)`. Both SIGTERM handlers (the loop's and host-apply's) are one-shot, so a second SIGTERM cannot cut the cleanup short, and the watcher's final SIGKILL of the child group always runs. host-apply holds a SIGTERM that arrives while it is spawning a command until the command's handle exists, so a signal landing between `Popen` returning and the handle being bound no longer orphans the command (a SIGTERM held during a spawn that then fails still exits 143), and an `OSError` while reading a started command kills its group before propagating. A signal landing inside `Popen` itself (between `fork` and its return) can still orphan a command; that narrow window is left to the unit's cgroup.
- A CRLF `fleet.lock.yaml` can be pinned (it was always refused as changed-since-read) and keeps its line endings; same-second lock backups no longer overwrite each other; an interrupted write no longer strands `<lock>.tmp`; a failed backup copy removes its empty `.bak` file.
- `herdr-eng fleet lock verify` no longer creates the watcher state directory or lock file when it does not exist.
- Audit of the #865 self-restart (no code change needed): a freshly started loop does not restart again while the succeeded rollout is still the latest saved one (the decision is taken only in the pass that ran the rollout), and unreadable or corrupt state means no restart and no crash. Real-process tests pin both.
- Tests: exact event tails instead of `in types[-2:]`, SIGTERM must exit 0. Operator doc: restart-mid-rollout safety, symlinked-lock backup location. Supersedes #825 (its own restart-token mechanism is dropped in favour of #865).
### Claude Code: blocking `PreToolUse` guard in the managed workflow hooks

- `herdr-eng workflow install|repair` now also registers a `PreToolUse` hook (matcher `Bash|Write|Edit|MultiEdit|NotebookEdit`, same `herdr-workflow:managed` marker and guard) that denies `Write`/`Edit`/`MultiEdit`/`NotebookEdit` under `/tmp` and `/private/tmp` (the harness scratchpad `/tmp/claude-<digits>/` is allowed), denies `git push` to `main`/`master` (explicit refspec, `HEAD`/`@`, or a bare push from a repo on that branch; it parses the whole command, follows `cd` and `git -C`, sees through subshells, keywords, `env`/`time`/`sudo` and `bash -c`, ignores redirections and heredoc bodies, asks when the target directory is unknown, and denies a plain `main`/`master` push it cannot tokenize), and asks before any other push from a repo whose `origin` contains `herdr-engineering`. It fails open and uses no network. Existing installs show as drift in `workflow doctor` until `workflow repair`. `ClaudeHooksUnit` gained an optional per-event `matchers` map. See `docs/operations/workflow-hooks.md`.

### Quieter client disconnects; fallback timeout no longer caps the body read

- Control-plane listeners (`_DaemonThreadingServer`) no longer print a stdlib traceback for `ConnectionResetError`, `BrokenPipeError` or `ConnectionAbortedError` (about 6,700 resets and 100 broken pipes per day in `herdr-cp-web`); they log at debug. Any other handler exception still logs its traceback.
- llm-proxy: the 10 s fallback-attempt timeout covered the whole upstream socket, so when a fabric answer arrived but no fallback was taken, a body slower than 10 s failed as `TimeoutError`. The socket is widened to the 600 s upstream timeout once headers arrive. Both values are now module constants (`FALLBACK_ATTEMPT_TIMEOUT_SECONDS`, `UPSTREAM_TIMEOUT_SECONDS`).

### Tier-1 ensemble checked by Opus, poll scope, Muse serve unit (always-on-review)

- `tiers.tier1.reviewers` (new, default empty = unchanged behavior): several open-weight reviewers run in parallel per PR head, `backend: gateway` (e.g. qwen3.8-27b) or the new generic `backend: openai` (base_url, model, `api_key_file`; llama.cpp such as Muse-Glimmer-30B on 127.0.0.1:28931, requests serialized per server). They reuse the eval's system prompt, prompt (diff + touched files, 60k cap) and JSON findings parsing. Tier 1 stays advisory: no status, no comment. A down reviewer is recorded and skipped.
- Tier 2 (Opus, `claude -p`) receives the deduped union (file + nearby line + title similarity) and answers `tier1_checks` (confirm or reject plus a one-line reason per finding) and may add findings. Only one comment per head SHA is posted, by tier 2: confirmed, Opus-only, rejected (collapsed in `<details>`), and which models found each. Tier 1 empty: Opus still runs normally.
- New scope keys (documented in `always-on-review.example.yaml`): `skip_archived` (true), `skip_forks` (false), `skip_drafts` (true), `only_prs_updated_after`, `backfill` (false), `max_comments_per_hour` (unset).
- BEHAVIOR CHANGE: a repo polled for the first time is enrolled at that moment and only PRs updated after it are reviewed (previously every open PR was). A repo the existing state.json already has review keys for (an upgraded deployment) is grandfathered with no cutoff, so its in-progress PRs stay in scope. Archived repos are now skipped by default. `backfill: true` and `skip_archived: false` restore the old behavior.
- `deploy/ci-review/herdr-muse-serve.sh` and `herdr-muse-serve.service`: example llama.cpp server-cuda-b11804 for Muse (not installed by anything).
- Rollback: delete `tiers.tier1.reviewers` (tier 1 and tier 2 behave as before), set `backfill: true` and `skip_archived: false`; or revert the PR. State keys added to state.json (`repo:*.enrolled_at`, `_comments`) are ignored by older versions.

### Fix: control-plane-web can reach the fabric whois sidecar for the phone bridge

- `tailnet_open_owner` failed closed on the live host because `herdr-cp-web` had no whois socket. `compose.collab-fabric.yaml` now mounts the sidecar's socket directory **read-only** at `/run/fabric-whois` in `control-plane-web` (only when that overlay is layered). Set `rakazo_bridge.whois_socket: /run/fabric-whois/whois.sock`. No other service, capability or network changes; the sidecar keeps its hardening. See `docs/operations/shepherd-discovery.md`.

### Fix: fleet-watch host-apply smoke checks resolve the launcher and use `autospec doctor`

- The rollout's first live pass (2026-11-17) halted on `beast/autospec` and would never converge: the autospec health smoke ran bare `autospec --version`, and (a) the autospec CLI has **no** `--version` flag (it exits 2 with `unknown autospec command`), and (b) neither a systemd unit's PATH nor a non-interactive ssh shell's PATH contains `~/.autospec/bin`, so bare `autospec` was not found at all. `pi` had the same PATH gap (`~/.local/bin` missing on the Linux ssh/service PATHs).
- The host-apply adapters now resolve the launcher as **PATH (the host env) -> the stable install location -> the bare name** (`autospec`: `~/.autospec/bin/autospec`; `pi`: `~/.local/bin/pi`), and the autospec smoke is **`autospec doctor`** (read-only, fast, exits 0 on a working install) instead of the nonexistent `--version`. The `HERDR_FLEET_AUTOSPEC_SMOKE_CMD` / `HERDR_FLEET_PI_SMOKE_CMD` seams are unchanged; a launcher that is nowhere still fails closed (`version_smoke` failed -> `health_failed` -> the rollout halts, nothing half-applied).
- Spec: the health-check table in `docs/specs/herdr-fleet-rollout-watcher-20261104/spec.md` now names the resolved launchers and `autospec doctor`.

### Fix: fleet-sync provisions the herdr-engineering deployment checkout the rollout rows require

- Second latent blocker found while converging the same first rollout: the herdr-engineering row resolves its DEPLOYMENT checkout from `$HERDR_ENGINEERING_REPO` or `~/.config/herdr-engineering/deployment-checkout` (non-interactive ssh has no profile env), and **nothing provisioned either** - the 2026-11-17 rollout would have failed closed with `no deployment checkout` on beast, bender, and macbook-pro alike (masked: it had already halted on the autospec row). The fleet-checkout clone existed on beast only.
- New `ansible/tasks/fleet-deployment-checkout.yml` (fleet-sync, before the watcher unit): clones the dedicated deployment checkout (`~/.local/share/herdr-engineering/fleet-checkout`, overridable per host with `fleet_deployment_checkout:`), ff's it to `main` (`update: true`, `force: false` - a dirty checkout fails loudly instead of being touched), and writes the one-line 0600 pointer file. It deliberately does NOT reuse `herdr_engineering_checkout` (fabric-agent.yml's set_fact repurposes that variable to the immutable per-revision release dirs, which are pinned to a sha and not on main - the D6 gate refuses them).
- Docs: `docs/operations/fleet-rollout-watcher.md` now lists the per-host deployment-checkout prerequisite. Structural tests: `tests/test_ansible_fleet_deployment_checkout.py` (task shape, ordering before the watcher unit, pinned repo/ff-only/main, pointer file path+mode, variable trap).

### Fleet watch: the watcher self-restarts after a herdr-engineering self-update

- After a fleet-watch rollout **succeeded** and updated the watcher's own herdr-engineering install, `herdr-eng fleet-watch loop --enable-rollouts` now exits with status 75; the unit's existing `Restart=on-failure` (75 is not in `RestartPreventExitStatus=2 3`) restarts the service on the newly installed build, so the fleet runs the build it just shipped (issue #865; spec D12 in `docs/specs/herdr-fleet-rollout-watcher-20261104/spec.md`). The restart comes from systemd itself - no detached `systemctl` child, which the unit's cgroup cleanup would race (review of #867), and an exit 0 would not restart the unit. It never requests a restart for a halted or running rollout, for a rollout that left this host's install unchanged, in plan-only mode, or when not running under the unit (dev runs and tests, detected via `/proc/self/cgroup` - the loop keeps running). `HERDR_FLEET_WATCH_RESTART_CMD` runs an injectable command for tests and dev; the loop still exits 75. See `docs/operations/fleet-rollout-watcher.md`.

### Shepherd tailnet discovery (zero-typing onboarding)

- New unauthenticated `GET /.well-known/herdr-shepherd` on the rakazo bridge: `{name, bridge_url, api_version, auth_modes, tailnet}`, no secrets. `tailnet` is true only when the real socket peer is identified by tailnet whois (fabric-trust `TailscaleWhois`); `X-Forwarded-For` counts only from `trusted_proxies`; whois errors fail closed.
- New `control_plane.rakazo_bridge` YAML keys: `tailnet_open_owner` (default off: whois-identified tailnet callers get the single-operator session from `POST /api/auth/open`, app Origin still required), `public_url`, `name`, `whois_socket`. `open_owner` and password sign-in are unchanged.
- Security hardening: tagged devices and this host's own addresses are refused; optional `tailnet_open_owner_users` allowlist; IPv4-mapped proxies and peers are unwrapped everywhere; whois decides whenever `tailnet_open_owner` is on (even with `open_owner`); repeated `X-Forwarded-For` headers are refused; whois errors cached 2 s with single-flight lookups; `bridge_url` only for tailnet callers.
- Docs: `docs/operations/shepherd-discovery.md`, `docs/operations/shepherd-onboarding-contract.md` (app-side probe order).

### Install: screenwright is installed and kept at the latest origin/main

- New `scripts/install-screenwright.sh` (wired into `scripts/install.sh` as step 7d, default on, non-fatal; `--no-screenwright` or `HERDR_INSTALL_SCREENWRIGHT=0` opts out) clones `berlinguyinca/screenwright` to `~/.local/share/herdr-engineering/screenwright`, fast-forwards it to `origin/main` on every run, runs `npm install` when HEAD moved, and calls the repo's own `ensure-install.sh` to link `screenwright-review` into the four harnesses. A dirty or non-main checkout is skipped with a message (exit 0). Prints `screenwright=<short sha> changed=<bool>`. `HERDR_SCREENWRIGHT_REF` pins instead of tracking. `lock/upstreams.yaml` records it as `revision: "tracking origin/main"`. See `docs/operations/screenwright.md`.

### Fix: inspector readers no longer wait on a durable write

- `InspectorStore.ingest` held the store-wide lock across the durable sink write. The sink serialises every write behind one asyncpg connection and a write lock, so under an ingest burst the lock holder waited behind unrelated writes and every reader (`agents/list` via `phone_digest`, `health`, `has_session`, `newest_seq`) waited with it; `agents/list` took up to 30 s on bender on 2026-11-17. The durable write now runs with the store lock released; ingest writers, `sweep`, `set_prefs` and checkpoint restore are serialised by a separate ingest lock (order: ingest lock, store lock, sink lock) and a reader sees the state from before an event until that event is durable, as before.
- Measured on bender (py-spy, 2300 samples over 100 s): readers blocked on the inspector lock in 10% of samples, the lock holder blocked on the sink write lock in 4%.

### Claude Code: the default engineering workflow registers itself as managed hooks
- New `herdr-eng workflow plan|install|status|doctor|repair|uninstall` (and `scripts/install.sh` step 7c, on by default; `--no-workflow-hooks` or `HERDR_INSTALL_WORKFLOW_HOOKS=0` opts out) writes two hooks into `~/.claude/settings.json`, marked `herdr-workflow:managed`: `SessionStart` prints a short engineering-defaults reminder (sonnet delegation, evidence before claims, branch -> PR -> CI, real deps, never push herdr-engineering, nothing under /tmp, Woodpecker CI, `/handoff`), and `PreCompact` prints a reminder to run `/handoff` first. Both only print, always exit 0, and are guarded by `[ -x <python> ] && ... || true`, where the command is `<installing python> -m herdr_engineering.cli` (not `herdr-eng` from `PATH`). The hooks never run `herdr-handoff`.
- Reuses the herdr-memory `ClaudeHooksUnit` with its own marker (`marker` class attribute), so foreign hooks and the herdr-memory hooks are untouched, writes are atomic and backed up (the first backup is kept pristine), a second run is a no-op, and invalid JSON is never rewritten. See `docs/operations/workflow-hooks.md` and `docs/operations/handoff.md`.

### Fix: `/api/auth/open` no longer waits on the audit write

- The open-owner sign-in queued its audit entry synchronously: `audit_record` holds the assistant store lock across the durable write and a full snapshot persist, so any contention there (a stall of 70.6 s was seen on 2026-11-17 against a normal 1.3 s) stalled the phone's first launch past its 8 s timeout. The entry now goes to a bounded background queue (256, drop-and-log on overflow, flushed on `RakazoBridge.close`); password sign-in and sign-out still audit inline. Rate limit and tailnet/origin checks are unchanged.
- The slow-call line for `auth/open` (and password sign-in) now ends with per-phase timings, e.g. `auth/open took 1275 ms (status 200, mint 1200 ms, audit 0 ms)`.

### Fix: `control-plane checkpoint` names the reason for any ValueError

- The CLI JSON error carried a `reason` only for `CheckpointInvalid`; any other `ValueError` (for example `invalid projection checkpoint schema`) printed just its type and stage, so a failing deploy prewarm was undiagnosable. It now prints `reason` for a `CheckpointInvalid` at any stage and for any other `ValueError` once the connection is open (migration, index preparation, materialization), redacted through the worker's `_safe_message` (URLs and DSNs become `<url>`, at most 500 characters). The `projection restore rejected; complete replay required` warning carries the same safe message. Exit code and `ok: false` are unchanged; `configuration` and `database_connection` stage errors (a malformed DSN can be quoted without `://`) and other exception types still print only name and stage.

### Fix: phone Assistants tab lists only online agent bots

- `bots/list` (and the bootstrap payload built from it) now returns only collab agents that are online; stale tokenless-join test agents no longer flood the tab. Nothing is revoked or deleted: an offline agent still resolves through `bots/get`, `threads/get` and `threads/send` by id. A separate list of offline agents is not exposed (the archived list means disabled assistants and `bots/restore` cannot restore an agent).

### Fix: recurring unit-gates flakes (pipelines 1428, 1444, 1446, 1453, 1454)

- Throwaway test Postgres clusters skip disk syncs (`fsync=off`, `full_page_writes=off`; commits stay durable across an immediate stop) and stop through `tests/control_plane/pg_stop.stop_postgres`: a fast stop that waits up to 300 s (pg_ctl's default is 60 s and exits 1 on timeout, which failed the owning test at teardown and left `the database system is shutting down` for the module's later tests), then an immediate stop if the postmaster is still up (restart helpers pass `fallback=False`). A guard test requires every launcher to use both.
- Shared `tests/io_timeouts.py` (`HERDR_TEST_IO_TIMEOUT`, default 30 s) replaces the 3-10 s literals on the local-server HTTP helpers and the 60-90 s CLI/deploy subprocess waits that failed on a starved node.
- `test_watchdog_does_not_fire_when_cycles_complete` uses a 2 s cycle timeout (was 0.5 s) and runs past it, so it still proves the cycle clock is refreshed.
- `scripts/ci/pytest-parallel.sh`: `-n auto` is capped by the effective cgroup CPU quota (smallest on the path to the root, v2 `cpu.max` and v1 `cfs_quota_us`; `scripts/ci/cpu-quota.py`) when it is smaller than the cap and the visible cores; `scripts/ci/host-facts.sh` prints host, CPUs, quota, load, memory and tmp filesystem at the start of the gate.
### Report preview: link-preview metadata, honest share notice, fairer section quotas

- Follow-up to #748 (merged before these review fixes): in a share, per-field truncation markers, the omission notice and the legend no longer point at the unshared descriptor, share wording never pushes the page over the cap, post-hoc replacements in the preview are removed, and quota rounding leftover is distributed instead of dropped.
- The report preview head now carries `description`, `og:title`, `og:description`, `og:type=article`, `og:site_name` and `twitter:card=summary` (summary cut near 160 characters, falling back to finding/evidence counts; every value attribute-escaped). The CSP and the sanitiser allowlist are unchanged. The tags are dropped only when even the 4 KiB minimum cap cannot hold them.
- In a share, the truncation notice no longer says "see the full descriptor" (that link was rewritten to plain text because the descriptor is not shared); it now says the full report is available to its owner.
- Section quotas: unused quota of a section that fits is now handed to sections that overflow, so a 1 MiB preview of 500 evidence rows no longer drops about 16 of them under the fixed 4/2/2/1 weight split. Output still never exceeds the cap.

### Fix: Woodpecker agent pruner and fabric firewall script polish

- `wpci-agent-prune`: a null or non-object queue response, a null `agent_id` in a queue task, or a non-list agents response now exits 1 with the normal `abort: API error` line instead of a traceback. The unreachable "more candidates than agents" abort is removed (candidates are a subset of the agents listed). README documents that after a >24h Woodpecker outage up to `--max-deletions` live agents per run may be removed (they re-register) and how to lower the cap.
- `collab-fabric-firewall.sh --check`: a missing iptables binary is exit 3 with a clear message (was folded into 2, "need root"); one chain absent plus the other unreadable reports absent (1). `deploy.sh` treats 3 like absent (the strong WARNING). `--install`/`--remove` unchanged apart from the message. Exit-code list in `docs/operations/fabric-trust.md` updated.

### Chore: scrub private infrastructure identifiers from tracked files

- Tailscale IPs, the tailnet name, and a personal hostname in docs, tests, examples and help text are now documentation placeholders (100.64.8.x, <tailnet>, operator-laptop). Git history still contains the old values; a history rewrite has not been done (owner decision). deploy/fabric-stack/config.yaml keeps the live tailnet name because the deployment mounts it.

- scripts/ci/main-cron-gates.sh no longer defaults HERDR_CI_STATE to a personal home path and now fails (exit 2) when it is unset, rather than guessing a non-persistent directory. .woodpecker/main-cron.yml sets it to $${WPCI_CI_DIR}/state/herdr; the runner must export WPCI_CI_DIR as the in-container mount of the shared CI store (the runner WPCI_CI_MOUNT).
- New ansible/playbooks/ci-runner-env.yml provisions that runner side: a systemd drop-in for the Woodpecker agent exporting WPCI_CI_DIR (WOODPECKER_ENVIRONMENT) and mounting the shared CI store at it (WOODPECKER_BACKEND_DOCKER_VOLUMES). Paths come from private inventory vars `wpci_ci_store` / `wpci_ci_mount` on group `ci_runners`; none are committed.

### Fix: deploy.sh no longer warns that the fabric firewall is missing when it merely cannot read it

- `collab-fabric-firewall.sh --check` run as the non-root deploy user exits 2 (cannot read), which `deploy.sh` treated like absent rules and always printed the install WARNING. Exit 1 (absent) keeps the WARNING; exit 2 now prints a one-line NOTE (verify with `sudo <script> --check`, noting if `herdr-collab-fabric-firewall.service` is enabled). Never fails the deploy. Docs in `docs/operations/fabric-trust.md` updated.

### Fix: unit gate no longer starves or hangs on the 1.77M-row recovery test

- `test_production_cardinality_snapshot_recovery_and_database_restart` (~5.5 min alone, >23 min under disk contention) is marked `slow` with an explicit `@pytest.mark.timeout(3600)`; the suite-wide 900 s ceiling failed pipeline 1334 after ~57 min.
- `scripts/ci/unit-gates.sh` runs everything else under xdist with `-m 'not e2e and not integration and not slow'`, then the `slow` tests serially in a tail pass (its hang limit is 4200 s so pytest-timeout names a hung test first), so coverage is identical and the slow test no longer competes with the workers.
- Both passes run under `scripts/ci/run-watched.py` (derived from the shared run-watched helper; it tracks the command's session, parent-pid descendants and any process carrying its per-run token in its environment, so a Postgres started by `pg_ctl` and reparented to init is counted and stopped; it reads per-process CPU from /proc, stops the whole tree on a hang or on SIGTERM/SIGINT/SIGHUP (exit 128+signal), gives survivors left after the command exits a bounded grace, and keeps draining if the reader of its output closes): a soft notice after `HERDR_PYTEST_SOFT_SECONDS` (3600), and a kill (exit 124) only after `HERDR_PYTEST_HANG_SECONDS` (1800) with no output and no CPU, so a wedged run (pipeline 1362, ~8 h) fails instead of hanging; a slow live run is never killed.
- Tests: real-subprocess coverage that slow tests are deselected from the parallel pass and run in the tail, that the two selections partition the default collection, and the watchdog's kill / no-kill behaviour. Rollback: revert the commit.

### Codex second reviewer on risky paths (always-on-review.py)

- New YAML block `risky` (default `globs: []` = off, behaviour unchanged). For a PR touching a path that matches `risky.globs` (`**` globs, case-insensitive, a rename counts by old and new path), `Opus agent review` is posted success only when a Codex review of the same head also approves: Opus runs first, and only if it approved cleanly `codex exec` (read-only sandbox, `--ignore-user-config`, strict verdict schema, same evidence rule) reviews the same worktree; the verdicts are merged and the script decides the state. A Codex block posts failure with its findings. If Codex cannot run or its answer cannot be verified (missing binary or login, time cap, bad exit, malformed answer, unverifiable block) the status stays pending, never success. Non-risky PRs and tier 1 never call Codex; a moved head posts nothing; dry-run and the two-key post rule are unchanged. Cost: `risky.codex.daily_budget_usd` with `input_usd_per_mtok` / `output_usd_per_mtok` (codex reports tokens only; the prices are required with a budget) is summed from `reviews.jsonl` (`tier: codex`) since 00:00 UTC and skips a risky review before Opus runs once reached. A Codex block whose blockers all cite files outside the PR also stays pending; `-c project_doc_max_bytes=0` keeps a PR-added AGENTS.md out of Codex's instructions; implausible token counts (negative, non-integer, over 10**12) fail the review. `show-command --tier codex` prints the command. Not installed or deployed by this change; beast needs `codex login`. Rollback: set `risky.globs: []` (or remove the block) and restart the poller.

### xdist follow-ups: collision-free ports, accurate comments, stronger parallel tests

- `test_allocation_skips_os_bound_port` and `test_failed_forwarder_start_is_retried_on_next_sync` bound fixed ports (18500, 24700) that could collide across files running concurrently under `--dist=loadfile`; they now use OS-assigned ports.
- `scripts/ci/pytest-parallel.sh` comments corrected: `loadfile` does not honour `xdist_group` (only `loadgroup` does), and `--maxprocesses` caps explicit worker counts too.
- The loadfile test now runs four real files on two workers and asserts more than one worker was used with each file on exactly one; added refusal tests for `HERDR_PYTEST_MAXPROCESSES` of `0` or non-numeric.
- `tree._live_processes` runs `ps -ww`: xdist workers export `COLUMNS=80`, which truncated process args (regression test added).
- `execnet==2.1.2` (what `pytest-xdist==3.8.0` resolves to) is pinned in the dev extra.

### Fix: leftovers from the review of the collab agents follow-ups

- `herdr-eng collab agents renew`: a failed write with `--json --print-credential` now prints the one JSON object (with `credential`) on stdout, not the bare credential; a failed move into place with `--print-credential` also prints the credential (or the JSON object) on stdout, keeping the 0600 temp file and its path on stderr.
- `deploy/control-plane/rooms-bootstrap.py`: a membership conflict is recognised by the real HTTP status (409), not by the text "HTTP 409" in the message, so a non-409 error quoting it is no longer swallowed. The missing-operator-id test now covers every falsy `user_id` shape against a real stub that would otherwise remove an unowned grant.
- Runbook 5.2a step 5: fabric-join agents are refused (403) and renewing an expired agent at the agent limit returns 429.

### Fix: collab agents CLI and rooms-bootstrap follow-ups, flaky MCP upload tests, renewal runbook

- `herdr-eng collab agents renew`: `--json --print-credential` now prints one JSON object that includes `credential` (no second line); a failed file write with `--print-credential` prints the credential on stdout as requested instead of stderr; the temp file is fsynced before it is renamed into place; a read timeout on an error body, a malformed `--operator-url` (`http://[::1`) and a listed agent without an id are one-line errors (exit 1), not tracebacks.
- Tests: the agent-cap test uses a real fixture (servers and threads are torn down) with a wider TTL margin; the disk-full test sets `RLIMIT_FSIZE` inside the child instead of `preexec_fn`; added coverage for the failed rename (temp file kept at 0600, path printed).
- `deploy/control-plane/rooms-bootstrap.py`: a 409 on one member no longer aborts the others (the run still fails at the end), and a missing operator id from `/me` refuses to remove any membership. The `REMOVED stale membership` count is now asserted.
- `test_collab_mcp_artifacts.py`: the raw-socket waits in the two load-sensitive tests are 30 s, so they pass under `-n 4`; assertions unchanged.
- Docs: new "Renewing an expired agent" runbook (section 5.2a) in the collaboration network guide. Rollback: revert the commit.

### Fix: retired identity-less registrations are terminal and keep host-level blockers (#779, #798 review)

- `fleet rollout retire-identityless` now settles only what it can: the identity refusal is recorded as a typed `blockReason` on the session (no string matching), and host-level reasons (unverified native coverage, ambiguous native identity, a missing discovered session) are recorded separately as `hostLevelError`, so a host with an identity-less session plus a coverage error stays blocked with its reason and `coverageErrors`. Hosts with nothing retired are untouched.
- A retired session is terminal: the runner no longer stops as blocked or stalled on it, its husk leaving the native inventory no longer raises "previously discovered session is missing", and it keeps its refusal in `error` and `retiredSessions`. If its identity resolves, the next pass un-retires it and replaces `pid`/`instanceId` with the real values.
- The command exits 2 when `stillBlocked` is non-empty. Rollback: revert the commit.

### Fix: daily budget hardening (always-on-review.py, #818 follow-up)

- Spend already incurred is now logged (`usage`) when a review's head moves after the model ran and when every pass fails with an unusable answer, so `spent_today_usd` counts it (the billed usage of an unusable later pass is counted too). A logged NaN or infinite cost is counted as zero instead of making the sum NaN (which never tripped the budget). An integer `daily_budget_usd` too large for a float (e.g. `10**400`) is refused as a config error instead of raising `OverflowError`. `reviews.jsonl` is read with `errors="replace"`, so an undecodable byte skips that line instead of crashing a review. Per-PR locking and the whole-log read are unchanged. Rollback: revert the commit.

### Daily budget per tier (always-on-review.py, #792 follow-up)

- New optional YAML key `tiers.<tier>.daily_budget_usd` (positive number; default unset = no limit, behaviour unchanged). Before starting a review the poller sums the `usage.cost_usd` that tier logged in `state_dir/reviews.jsonl` since 00:00 UTC today (partial and dry-run reviews count; other days and the other tier do not). At or over the budget the review is skipped with one log line (`daily budget exhausted`) and the status is left exactly as it was (never success, no new pending); `--force` does not bypass it. Reviews already finished are unaffected. Zero, negative, non-numeric, boolean, NaN and infinite values are refused at config load. Rollback: remove the key and restart the poller.

### Reviewer fixes: bare clones, optional green-only gate, diffs over 20,000 lines (always-on-review.py)

- Fix: the `clone:` check required `<clone>/.git`, so the bare clone the README recommends was refused. It now asks git (`is_git_clone`): a bare or non-bare repository root passes; a missing path, a plain directory, a directory inside another repo or a dangling `.git` is refused.
- Fix (#790, diffs over GitHub's 20,000-line limit): when `gh pr diff` fails because GitHub refuses the diff as too large (HTTP 406), the always-on reviewer now builds the diff locally in the detached worktree (merge base to the pinned head, the same three-dot diff), so the in-PR file index, evidence checks and prompt work as usual. Any other `gh pr diff` failure still fails the review. This covers the 20,000-line limit only; the separate `github-fleet-automerge` 120 KB per-file limit named in the issue body lives in a service outside this repository and is not changed here.
- Default change (operator approved): tier 2 no longer waits for green checks; it reviews every non-draft head. New YAML key `gates.wait_for_green` (boolean, default `false`) restores the old gate; `gates.required` is only needed, and only validated as non-empty, when it is `true`. Cost note: Opus now runs on heads whose CI is still pending or red, so expect more reviews. Rollback: `gates: {wait_for_green: true, ...}`; restart the polling service after editing, it reads the config at start.

### Fix: Independent AI review poller waits for a busy single-slot model

- `ai-review.py`: a 429 from the gateway (deepseek and qwen have one slot, so any parallel caller gets 429) used to share the 4-retry budget (about 75 s) with 5xx errors, shorter than one chunk's generation time, so a busy slot failed the review. 429 is now waited out for up to the new `reviewer.busy_wait_s` (default 1800 s of total waiting, each wait capped at 60 s, `Retry-After` honoured) and no longer spends `retries`. When the budget runs out the run fails and posts no success (and no terminal status). Config: `reviewer.busy_wait_s` (>= 0). Rollback: revert the commit. `reviewer.backoff_s` must now be greater than 0, and backoff waits are capped at 60 s.

### Collab: `herdr-eng collab agents list|renew`

- New CLI over the collab operator listener (`HERDR_COLLAB_OPERATOR_URL`, default `http://127.0.0.1:9482`): `collab agents list` shows id, name, harness, status, expires_at and `renewable`; `collab agents renew <agent-id|@name>` renews via `POST /api/v1/collab/agents/{id}/renew` and writes the NEW credential only to `--credential-file` (0600, no overwrite without `--force`, checked before renewing) or to stdout with `--print-credential`; the credential is never logged or in `--json`. After renewing, update the harness config (`claude mcp add` header, `~/.codex/config.toml`, collab-spawn credential file).

### Collab: owner renew of expired agents, sliding window on use

- New `POST /api/v1/collab/agents/{id}/renew` (`CollabService.renew_agent`, dashboard **Renew access** on an expired agent card, shown only when the agent list says `renewable`): the signed-in owner (404 for anyone else, 403 for an agent caller) gets a new credential for the same agent id, device and name, shown once, with expiry now + `remote_credential_ttl`. The old credential dies at once. Refused: revoked (409), helper, fabric-machine, fabric-join and externally bound agents (403; an external agent's `hard_expiry` is never extended). It reuses the `credential_rotated` event with action `agent.renew`, so replay and audit work unchanged; a renewal of an expired agent counts against the agent cap again (429).
- Room grants have their own expiry and the service drops lapsed ones, so renew also restores access: every room grant of the agent that lapsed is re-granted with the same actions, and every live one that would lapse before the new credential is extended to it (reply field `grants_restored`). Only rooms the owner owns; team-attached and coordinator grants are never overwritten or brought back (attaching a team or designating the agent coordinator replaces a lapsed direct grant for good), and a removed member's lapsed grants and a revoked agent's are forgotten. A restored grant expires with the new credential. After a grant lapsed, removing it answers 404, so the owner cannot veto the restore short of revoking the agent or removing the member. The lapsed actions are rebuilt from the event log on replay.
- Agents made by `connect_agent` (new `origin: "connect"` on `agent_enrolled`; older ones get their cap at their first slide) now slide: a successful Agent auth moves `expires_at` to now + `remote_credential_ttl` once less than half the window is left (about two `credential_rotated` events with action `agent.slide` per window), best effort (a full log never fails the request), never past an absolute cap of 180 days from creation or the owner's last renew (`AGENT_MAX_LIFE`), never for helper/fabric/external agents and never for an expired or revoked one. Room grants are not slid. A stolen credential in regular use lives until revoked or the cap.
- `/rooms/{id}/agents` lists each grant's `expires_at`, and `/agents` a `renewable` flag.
- `deploy/control-plane/rooms-bootstrap.py` renews an expired operator-owned agent in place (`RENEWED agent <handle>`, the credential is dropped unread) instead of removing its grant and connecting a new agent, and treats a listed grant whose `expires_at` has passed (housekeeping lags up to 30s) as absent, re-granting it; foreign-owner handling is unchanged, and a refused renew falls back to creating one.

### Review prompt v2: rules, checklist, evidence, passes, replay harness (always-on-review.py)

- New YAML keys: `prompt.project_rules` (trusted rules in both tiers' prompt), `prompt.checklist_file` (`deploy/ci-review/review-checklist.yaml`, validated, the reviewer lists the ids it considered in `checked_items`), `prompt.require_evidence` (default true), `tiers.tier2.passes` (the example ships general, tests, security, migrations; unset or `[]` keeps one unfocused review) and `replay.clones`. The claude prompt now tells the reviewer to read callers, tests and migrations before the verdict.
- Findings gain `evidence` and `how_it_fails`; the script downgrades a blocking finding to a note when its evidence is absent from the diff and the file at the pinned head. Old-shape answers still validate. `reviews.jsonl` lines gain `downgraded`, per-pass `passes`, `checked_items` and a capped, redacted `findings` array.
- New `replay --corpus DIR --tier tier1|tier2` (never posts, resumable, one at a time), `replay compare A B` and `corpus build --repo --since --out` (read-only `gh`). Two seed cases in `tests/ci_review/corpus/`.
- Review fixes: `time_cap_s` is one cap shared by all passes (each pass gets an equal share of the time left; the example tier 2 cap is now 3600). A later pass failing keeps the completed passes' findings (logged, `partial`); it never posts success. With `require_evidence`, a blocker now also needs `how_it_fails` and a quote of at least 20 characters or a whole line from the cited file's hunks; the downgrade count appears in the status text, and a `block` verdict whose blockers were all downgraded leaves tier 2 pending (tier 1 posts an advisory success). Merge dedupes on file, line, category and reason prefix. Over-long evidence is truncated instead of failing the verdict. Evidence quotes may keep their diff `+`/`-` markers and an `a/`/`b/` path prefix, removed lines and deleted files count, a short quote must be a whole line of at least 12 characters, boilerplate lines (`except Exception as e:`, `from __future__ import annotations`, `raise NotImplementedError()`) are never evidence, and a blocker on a file outside the PR is dropped rather than left pending. Replay marks a case whose pass failed `partial` (not scored, retried, exit 1), and `corpus build` records the PR's final head as `head_sha_fallback` for replay when the pinned commit is gone. Upgrade note: an existing config with `time_cap_s: 1800` should raise it to 3600, because the cap is now shared across passes. Replay retries skipped cases and re-scores a case whose ground truth changed. `corpus build` pins each case to the commit the comments were made on, counts only human or `replay.reviewers` thread-root comments and skips the PR author's.
- Default change: with the shipped example config tier 2 now makes four sequential claude runs, so cost and time per review rise about fourfold. Rollback: set `tiers.tier2.passes: []`, `prompt.require_evidence: false`, `prompt.project_rules: []`, drop `checklist_file`. Restart: the polling service reads the config at start, so restart it after editing.

### Always-on review: two tiers, claude and pi backends (dry-run by default)

- New `deploy/ci-review/always-on-review.py` builds on `ai-review.py`: tier 1 `Open-weight pre-review` (advisory, pi) and tier 2 `Opus agent review` (claude, only for heads with all gates green), pluggable `gateway` / `claude` / `pi` backends chosen in YAML only, strict verdict parsing, one review per head SHA, cooldown, time cap, detached read-only worktree, scrubbed environment. Posting needs `post: true` in config and `--post`. Nothing is installed or enabled; added example config, systemd --user service/timer and a herdr pane tail script. `ai-review.py` gained an optional schema argument on `Gateway.chat` and `Gh.check_runs`.

### Docs: ruleset now requires `Opus agent review`

- Ruleset 25691548 requires the commit status `Opus agent review`, posted by a Claude session after an Opus subagent reviews the exact head (re-post after any push). `Independent AI review` (`ai-review.py`) is advisory; the different-model-family rule no longer holds. Docs only.

### Inspector history reads the archive too

- `GET /api/v1/sessions/{id}/inspector/history` now continues into `fabric_events_archive` once the requested range goes past what the archive job moved out of `fabric_events`. The two tables are merged under the one keyset order and the same opaque cursor, so a page can straddle the boundary with no gap and no duplicate (a row present in both is returned once). No new setting: with no archive table, behaviour is unchanged. Replaces the earlier caveat that archived rows were not read; the archive job itself stays off by default.

### Fleet rollout watcher step 5: service, lock pinning, resume/abandon (off by default)
- `herdr-eng fleet-watch loop` (poll + plan; rolls out only with `--enable-rollouts`; flock single instance; soft limit warns, hard limit exits only after no progress), user unit `herdr-eng-fleet-watch.service` and `ansible/tasks/fleet-watch.yml` (disabled unless `fleet_watch_enabled: true` on beast). `lock.pin_repos` moves `fleet.lock.yaml` only after a full success with the D3 published-history check, atomically with a backup; `herdr-eng fleet lock verify`. `fleet-watch resume|abandon` (the `run --resume` flag is a deprecated alias). New reasons `downgrade`, `lock_invalid`. Nothing is enabled by this change. See `docs/operations/fleet-rollout-watcher.md`.

### Web: configurable GC thresholds, boot-heap freeze (`control_plane.gc`)

- `control_plane.gc: {gen0, gen1, gen2, freeze}` tunes the collector at web start (restart required). Keys left unset: `gen0`/`gen1` keep the running interpreter's values; `gen2` defaults to 1000.
- Behaviour change with no `gc` block at all: the default `gen2` goes from 10 to 1000, and the boot heap is frozen. Rollback: `gc: {gen2: 10, freeze: false}` in the fleet YAML.
- Values must be integers from 1 to 2147483647; a bad block stops startup with `--fleet-config: control_plane.gc...`.

### Design: always-on review pane

- Added `docs/superpowers/specs/2026-11-13-always-on-review-pane-design.md`, a proposal for a dedicated two-tier review process (advisory open-weight first pass, Opus on mergeable heads posting `Opus agent review`) with measured open-weight evidence, a replay-set plan, failure modes and rollback. Docs only.

### herdr-handoff: one place resolves the pin, and doctor checks what the CLI can actually do

- `scripts/install-handoff.sh` is now the only thing that installs or refreshes the required herdr-handoff dependency: it reads the revision from `lock/upstreams.yaml` (`HERDR_HANDOFF_REF` overrides for a controlled rollout, `HERDR_HANDOFF_REPO` installs from a local checkout as `scripts/install.sh` used to), fetches it into `~/.local/share/herdr-handoff/<rev>`, installs the CLI into `HERDR_HANDOFF_PYTHON` (or via `uv tool` where there is no pip), links the four harness skills, prints the revision it resolved, and ends by attesting that the entrypoint on PATH reports the version the pinned tree declares - so an install that left the old CLI on PATH fails instead of reporting success. `scripts/install.sh` step 3 calls it instead of declaring the revision a second time: install.sh carried `HERDR_HANDOFF_REF:-c425453` beside a lock entry naming the same commit by hand, so bumping the pin in the lock changed the reported status of every host and none of what they installed. `--plan` prints the pin, the resolved cache, the interpreter, the four skill directories and the PATH directory, and touches nothing.
- Nothing that is not the pin is overwritten. What is on PATH is judged by what it reports, not by what it is: an entrypoint already answering the pinned version is left alone (that is where a pip `--user` install lands on Linux, every run), and anything else that is not this script's own - a regular file, a link into another tree, a directory - is reported with exit 2 and untouched; `--migrate-copies` moves it to `herdr-handoff.pre-<rev>.bak` first and refuses rather than nest a backup inside an existing one. Linking the entrypoint onto itself - what happened once against a real `~/.local/bin`, leaving a working CLI answering "Too many levels of symbolic links" - cannot happen: candidates and destination are compared with every symlink in their parent directory resolved (the last component is kept as named, and a PATH entry that is itself a link is judged by where it points), because `/tmp` and `/var` are symlinks on macOS and a comparison that skipped that resolution looked fine in tests while linking nothing on a fresh host. Links are made with `ln -sfn`, so a PATH entry that is a symlink to a directory is replaced instead of having a new link written inside that directory.
- `fleet update` refreshes the dependency on the controller as a non-fatal `controller_handoff_refresh` step, pinned to the interpreter running `herdr-eng` (`HERDR_HANDOFF_PYTHON`) rather than whatever `python3` the caller's PATH resolves, and reports which interpreter it used. Where there is no pip the script falls back to `uv tool install --python <that interpreter>`, because uv choosing its own interpreter would make that report a lie; uv still keeps the tool in its own environment, and the PATH attestation is what proves the pin is in effect. Fleet hosts are **not** converged by this step and nothing probes herdr-handoff on a remote host yet - that gap is issue #799. A host whose handoff install is broken still syncs, and the failure is reported in `warnings` rather than blocking the fleet.
- `check_handoff_dependency` no longer reads "the binary exists" as health. It asks the CLI `--version` and `--help` and fails when a flow `herdr-eng` actually calls (`create`, `launch`, `takeover`, `confirm`) is missing, naming the version found and `scripts/install-handoff.sh`. A CLI it cannot run reports no capabilities at all; a command named inside argparse's `{a,b,c} ...` set counts, one named only in the description of another subcommand does not, and an unrelated brace group (an option like `--mode {fast,slow}`) does not disable the fallback that reads the rest of the help. Doctor keeps mapping the required dependency to a warning unless run strict, so an old pin now reads as a gap instead of a green check.
- Pin advanced to `a693217` (herdr-handoff#6, client 0.3.0): same-tab takeover where the close of the replaced pane is gated on herdr observing the successor live, plus `herdr-handoff confirm` for that successor. The previous pin, `c425453`, was client 0.2.0, which has neither command - the revision `herdr-engineering` declared required, and which `herdr-eng doctor` passed, could not perform the flow this repo drives.
- Known gap: a skill left as a plain copied directory by an install from before the linker is still reported as a collision, and its text never updates. `scripts/install-handoff.sh --migrate-copies` moves each such directory aside as `<skill>.pre-<rev>.bak` and links the pinned tree in its place; it is opt-in because a copy may carry local edits.

#### Install and doctor details

- `HERDR_HANDOFF_REF` accepts a short SHA (7-39 hex characters): it is looked up in the clone after refreshing branches and tags, since GitHub refuses to fetch a commit by abbreviated name; branches and tags are still fetched by name.
- `check_handoff_dependency` trusts the output of `--version` and `--help` only from a run that exited 0 with output; otherwise it reports `could not run the herdr-handoff CLI (<exit status>)` instead of claiming commands are missing.
- Removed from `scripts/install.sh` step 3: the sibling `../herdr-handoff` checkout default (`HERDR_HANDOFF_REPO` is the explicit way to install from a local checkout) and the editable `pip install -e` of that checkout into the project venv; the pinned tree is now installed non-editably by `scripts/install-handoff.sh`, so edits in a checkout no longer take effect without re-running it. A stale regular file at `~/.local/bin/herdr-handoff` (an old pip `--user` install) no longer aborts the whole install: step 3 warns, names the file and how to remove it (or `--migrate-copies`), and continues with the CLI unattested; the install then ends with a repeated warning and **exit status 3** ("installed with warnings: herdr-handoff CLI not attested") instead of 0, so scripts notice. An ambiguous short-SHA `HERDR_HANDOFF_REF` now says "ambiguous ref <x>: use a longer SHA".
- The revision cannot be read from `lock/upstreams.yaml`, or the pinned tree declares no static `version =` line and the CLI install was not skipped: both stop the script with the file named, because without them there is nothing to attest against.
- `scripts/install.sh` step 7 no longer repoints `~/.local/bin/herdr-eng` or `herdr-handoff` when either is a file, or a link into a tree the installer did not create; it says what the entry points at instead. Step 3's attestation is what proves the pin is in effect, so an entry it did not produce is reported rather than silently repointed.
- `HERDR_HANDOFF_SKILL_ROOT` means the same thing here as it does to the upstream installer - the parent of the per-harness directories - so `--plan` names the directories skills actually land in and `--migrate-copies` scans those. It used to mean the skills directory itself, which made `--plan` report a directory nothing was ever written to.
- A dangling relative symlink on PATH is reported as the collision it is; it used to stop the script with a raw `cd: No such file or directory` from the path resolver.

### Test fix: cargo target-storage CLI test no longer depends on ambient CARGO_TARGET_DIR

- `test_target_management_cli_enable_and_migration_safety` inherited the host environment; a CI agent exporting `CARGO_TARGET_DIR` or `CARGO_BUILD_TARGET_DIR` made the migrator report "Explicit target setting remains authoritative" instead of "same filesystem". The test now drops those two variables from the child environment and separately asserts the explicit-override verdict. No production code changed.

### Rakazo default assistant: review fixes
- Bots: every bot-meta read-modify-write (archive, restore, set-computer, mark-unread, sections, default flag) runs under one reentrant lock, so clearing the default can no longer drop `archivedAt`. Archiving clears `isDefault`; `bots/update` with `isDefault: true` on an archived bot returns 409. Tests: `tests/control_plane/test_rakazo_bots.py`.

### Inspector snapshot boot: used on the complete-replay fallback, strictly validated

- With projection checkpoints on (the default), a missing or rejected checkpoint fell back to a complete replay that ignored `inspector_snapshots` and re-ran every stored inspector row through redaction. That fallback now hands the stored snapshot to the repo, which verifies it by count and ordering and replays everything on any doubt.
- `control_plane.inspector_snapshots` is validated in `fleet_config.py`: `enabled` must be a boolean, `interval_seconds` a number >= 60, unknown keys are rejected (previously a bad interval was silently replaced by 600). Default stays off.

### Auto-join hook no longer dies silently on a loaded host
- `summary join-hook` ran the join under a 12 s kill-timeout through `python -m herdr_engineering.cli`; on a loaded host that start alone took 3-40 s, so the join was killed and the session printed nothing. It now starts the CLI directly, waits up to 8 s, and past that leaves the (idempotent) join running detached and says "still running in the background". Test: a real listener that never answers.

### Inspector: older events on demand from Postgres
- New `GET /api/v1/sessions/{id}/inspector/history?before=&limit=&view=` returns inspector events older than the in-memory ring, one bounded page at a time (opaque keyset cursor on `ingest_timestamp, sequence, event_id`), rendered with the ring's role redaction. Nothing is read at boot and nothing is loaded into the ring, so memory does not grow with paging. Existing `/inspector` and `before_seq` are unchanged. Configured by `control_plane.inspector_history` (`enabled`, `page_size`, `max_page_size`), validated in `fleet_config.py`, unknown keys rejected. Client: `getInspectorHistory`.
- Caveats: durable rows carry no ring `seq` (`seq` is null; `history_id` is the event id); a tool call's newest row per page wins, so a call split across a page boundary can show once per page; rows already archived to `fabric_events_archive` are not read.

### fabric_events partitioning (disabled by default, lazy-loading PR 9)

- Migration 0053 prepares an empty LIST-partitioned copy of `fabric_events` (inspector partition plus default); nothing moves automatically. New `control_plane.fabric_partitioning` config (YAML only, validated in `fleet_config.py`) and `control-plane fabric-partition status|backfill|cutover|rollback|drop-old`: resumable keyset backfill, one-transaction cutover with a bounded lock and exact catch-up, and a rollback that carries post-cutover writes back. `control-plane checkpoint` skips the CONCURRENTLY index on a partitioned journal. See `docs/operations/fabric-events-partitioning.md`.

### Rakazo bridge: `open_owner_origins` (default unchanged)
- New optional `control_plane.rakazo_bridge.open_owner_origins` (list of 1-8 `scheme://[host]` strings, default `["rakazo://"]`): the exact `Origin` values `open_owner` accepts, so a second native app can use it. http/https, wildcards, whitespace and duplicates are refused at startup; every other guard (network allowlist, proxy checks, 30s interval, operator account) is unchanged. Tests: `tests/control_plane/test_rakazo_bridge.py`.

### CI: unit-gates runs pytest in parallel (pytest-xdist)
- `scripts/ci/unit-gates.sh` now runs `python -m pytest -q -n auto --maxprocesses=8 --dist=loadfile` (arguments come from `scripts/ci/pytest-parallel.sh`), so the ~90 minute serial run is spread over workers; a file's tests stay on one worker. Overrides: `HERDR_PYTEST_WORKERS=<n>|auto|0` (0 = serial as before), `HERDR_PYTEST_MAXPROCESSES=<cap>`. The file-descriptor floor still applies first (the limit is per process, so each worker leaks only its share). Nothing is deselected or skipped. `pytest-xdist==3.8.0` is pinned in the `dev` extra. Tests: `tests/test_ci_pytest_parallel.py`.
- `join.sh --stop` now uses `ps -ww`: xdist workers export `COLUMNS=80`, which made `ps -o args=` truncate the worker's command line before `worker.sh`, so `--stop` silently stopped nothing (`tests/control_plane/test_collab_join_script.py` failed only under xdist).

### Fleet rollout watcher step 4: host-apply and the serial runner (manual, `--apply` required)
- `herdr-eng fleet host-apply` (precheck/apply/health/rollback per repo, D6 checkout gate, idempotent `no_change`, `fleet-auto-update.lock`, atomic Pi `current` switch keeping 3 releases) and `herdr-eng fleet-watch run [--apply]` (serial beast -> bender -> macbook-pro, halt on first failure, durable crash-resume, ssh timeouts, D8 rollback for pointer flips and ff-only, events through the existing emitter/outbox). Output and events carry no free text. No systemd unit (step 5). Tests: `tests/test_fleet_host_apply.py`, `tests/test_fleet_watch_run.py`. See `docs/operations/fleet-rollout-events.md`.

### Work Command Center: rollout gate `work_center_v1` (off by default)
- New capability `work_center_v1` in `/api/v1/health`, only when `control_plane.work_center.enabled: true` (YAML, strict `true`, default off). It gates the Overview "This week" card, the session "Work context" header and the GitHub board "Open in Herdr" links; with it off the dashboard is unchanged (the board no longer reads `/api/v1/mission-outcomes`). Rollback is turning the flag off. Nothing is deployed or enabled by merging. See `docs/specs/herdr-work-command-center-wc001/OPERATOR.md`.

### Control-plane client key for the llm services (optional)
- `ControlPlaneClient` (llm-proxy, `llm-gateway-sync`, `llm-gateway-usage`) sends `X-API-Key` read from an optional key file on every call; no file = no header = unchanged. The file must be mode 0600 (group/other-readable, missing or empty is refused without printing it). Setting: `--control-plane-key-file` on the three commands, plus `control_plane.llm_proxy.control_plane_key_file` for the llm-proxy slice; compose mounts `HERDR_CP_CLIENT_KEY_FILE` read-only when set.
- The `agent` role may now `POST /api/v1/token-usage` (viewer still may not; an open control plane is unchanged), so usage reporters need no operator key. Tests: `tests/test_control_plane_client_key.py`, `tests/control_plane/test_agent_token_usage.py`. See `deploy/control-plane/README.md`.

### Dashboard proxy for keyed control planes (`herdr-eng control-plane dashboard-proxy`, off by default)
- With `control_plane.api_keys` on, the browser dashboard got 401 on everything (no browser code sends a key; `EventSource` cannot). New same-origin reverse proxy `herdr_engineering/control_plane/dashboard_proxy.py`: GET/HEAD/static/SSE use a `viewer` key (SSE streamed unbuffered), writes use the `operator` key only when `METHOD /path` is in `control_plane.dashboard_proxy.operator_post_allowlist` (default = the dashboard's write actions; others 403 at the proxy; encoded slashes and dot segments refused). A client-supplied `X-API-Key` and `X-Forwarded-*` are never forwarded; hop-by-hop headers are stripped.
- YAML only: `control_plane.dashboard_proxy {viewer_key_file, operator_key_file, upstream, operator_post_allowlist}`, validated by `fleet config`. Key files must be mode 0600; the proxy exits 2 on a group/other-accessible, missing or empty file. Optional compose profile `dashboard-proxy` (port `HERDR_DASHBOARD_PROXY_PORT`, default 8081). With no block configured nothing changes. `API_KEYS_WARNING`, the README and `fleet-config.example.yaml` now say the dashboard works behind the proxy. Tests: `tests/control_plane/test_dashboard_proxy.py` (real keyed `ControlPlaneServer`).

### Ingest callers send the host config's `ingest.api_key`
- The feed-plugin `emit.js` and the fabric agent (service lease and host telemetry POSTs) now send `X-API-Key` when `ingest.api_key` is configured, and nothing when it is not, so they keep working once `control_plane.api_keys` is enabled. The agent follows key changes through `retarget`. Tests: `deploy/herdr-cp-feed-plugin/emit-send.test.js`, `tests/test_fabric_agent_ingest_key.py`.

### Optional `X-API-Key` for control-plane callers that sent none
`collab-spawn.py --api-key-file` (ingest report: ingest role; scheduler: operator role), `herdr-eng fleet drift`
(viewer; `--api-key-file` or this host's agent/ingest key), `herdr-eng artifacts upload --api-key-file` (a bare
`POST /artifacts` needs the operator role) and the `herdr-artifact` wrapper (`history` viewer, `share-request`
operator) now send a key once `control_plane.api_keys` is enabled. No key = no header = unchanged open behaviour.
Key files must be mode 0600 and are refused otherwise; the key is never printed. The duplicate
`_cmd_art_upload` in the CLI was removed (the two copies were identical).

### Build-cache report sender (`herdr-eng build-cache-report`, disabled by default)
- New `herdr-eng build-cache-report [--once|--loop] [--json]` schedules the existing `report_builds` sender. Config is YAML only, `fabric_build_cache.report {enabled: false, url, key_file, interval_s: 60 (10..3600), host_id}`; the ingest-role key is read from `key_file` only (never argv, env or logs). It refuses to run (exit 2) when disabled, when the url is not https (http only for 127.0.0.1/localhost), or when the key file is missing, empty or group/world readable. `--loop` logs one line per attempt and survives an unreachable or refusing control plane.
- Opt-in service files, enabled by nothing: `scripts/herdr-eng-build-cache-report.service` (systemd user) and `scripts/com.herdr-engineering.build-cache-report.plist` (launchd template). Runbook: `docs/build-cache-deployment.md`. Tests: `tests/test_build_cache_report_cli.py`.

### Dashboard report viewer and share panel: phone-width and dark-theme polish
- The dashboard's "Evidence and source downloads" table stacks into labelled blocks below 600px (table roles kept) instead of scrolling sideways with no cue.
- Share panel at 390px: the four tabs stay on one row; labels stack caption over control with a gap (Expires, Version, Audience, Compare, Report version, Finding or figure); the Links heading and Title field have top margin.
- Dark theme: the report detail's textareas, inputs and the "Evidence supporting this check" fieldset use the theme tokens (`--surface`, `--border`) instead of the browser's flat grey and white border. `.hd-note` is now styled.
- Public and preview report pages: roomier table rows (12px/14px padding, 1.55 line height); permalink focus ring uses `--link` (it was 2.99:1, below 3:1).
- Tests: `tests/playwright/viewer-phone-polish.test.mjs` (chromium, firefox); the share fixture now returns the descriptor's assets.

### Inspector archive job (disabled by default)
- New `control_plane.inspector_archive` config (YAML only, validated in `fleet_config.py`) and `InspectorArchiver`: moves inspector tool/message rows older than 14 days from `fabric_events` into `fabric_events_archive`, chunked, one transaction per chunk, idempotent, with a count-only dry run. Never moves rows above the snapshot watermark or projection checkpoint frontier, v2-envelope rows, or state-bearing kinds. Migration 0052 adds a partial archive index. Snapshot boot counts archived rows; the compaction archive purge now skips inspector rows. See `docs/operations/inspector-archive.md`.

### Fleet config: unknown keys rejected, `ci_deployments` fails closed
- `fleet config` loading now rejects unknown top-level, per-section and per-host keys (it dropped them silently; a live `control_plane:` written at the top level instead of `sections.control_plane.values` went unnoticed). The error names the key, never a value, and hints `sections.control_plane.values` for a top-level `control_plane`. Allowed: top level `version, sections, hosts`; section `roles, values`; host `roles, token_sha256, overrides` (everything the repo's own writers produce). While the control plane is running, a rejected edit leaves the last good config serving; a restart (or any CLI that loads the file) with such a key fails instead, so run `herdr-eng fleet config validate` before deploying.
- `control_plane.ci_deployments` is validated at load (`section control_plane: control_plane.ci_deployments: ...`) and is read through `FleetConfigStore.secure_section`: while the fleet file is unreadable, deleted or invalid the CI allowlist is empty (CI hosts get 403) unless `control_plane.config_failure_mode: last_good`. Deployment definitions still keep the last good config.
- The `kind: "ci"` badge and `definitions.ci_components` appear only when definitions are loaded. The evidence link no longer ends in `.,;:)]`. Tests: `tests/test_fleet_config.py`, `tests/control_plane/test_ci_deployments.py`.

### CI-reported deployments (`control_plane.ci_deployments`)

- New fleet config key `control_plane.ci_deployments: {component: host id}` lets a CI pipeline (for example Woodpecker as host `woodpecker-ci`) report deployments of components that have no `deploy/pipelines` definition, without a fake definition. An event is accepted when the definitions rule allows it or the exact (component, host) pair is listed; with deployment definitions loaded, another host reporting a listed component gets 403 (with none configured ingest is unrestricted, as before). Bounded (32 entries), validated, hot-reloaded; a component that also has a definition is dropped from the list (only that entry) and reported in `definitions.ci_errors`. With no definitions configured nothing changes; with definitions configured but invalid, every event is still refused (503), listed components included.
- The coordinator, plans, permits, escalations and force/ack ignore these components. The Deployments snapshot adds `definitions.ci_components`, `definitions.ci_errors`, and `kind: "ci"` plus a `ci` summary on their rows; the page shows them under "CI-reported deployments" with a badge, last deploy, outcome, commits and the evidence link. Documented in `deploy/control-plane/README.md`. Tests: `tests/control_plane/test_ci_deployments.py`, `tests/playwright/deployments.test.mjs`.

### Report viewer guide

- New `docs/artifacts/report-viewer-guide.md`: the Rich view opt-in, finding anchors, printing and saving as PDF, the "How to read this report" legend, phone-width Evidence and Downloads tables, and keyboard use of the share panel tabs. Linked from `docs/artifacts/share-links.md` and the README. Docs only.
- Fact-check fix: the guide now states the exact Rich view eligibility rule (one `text/html` asset, or exactly one with role `preview` among several) and when the legend is omitted.

### `/herdr::join` works with no flags after `herdr-eng summary install`
- `rooms join-fabric` found the listener only in the fetched host config, which agent hosts do not carry, so a bare call failed. It now also reads `fabric_join` (`control_plane_url`, or `listen_host` + `listen_port`) from the machine config `~/.config/herdr-engineering/config.yaml` and falls back to the readable fleet config's `control_plane.collab.fabric_trust`. YAML only, no new environment variable; `--control-plane` still overrides.
- `herdr-eng summary install` (and `scripts/install.sh`) records the fleet config's listener under `fabric_join` idempotently and never overwrites a differing value (reported as `preserved`). New read-only `herdr-eng summary install --check` verifies per harness the skill symlink, the OpenCode command, listener resolution and reachability, and the hook state, exiting 1 on a failure; no secrets are printed.
- Opt-in auto-join, off by default: `fabric_join.auto_join_on_session_start: true` installs one Claude Code `SessionStart` hook (appended, manifest tracked, removed by uninstall; HerdR panes only; never fails the session). Codex, OpenCode and Pi get no hook (no verified safe contract) and are reported `unsupported`. Installing with the key false installs nothing.
- Tests: `tests/control_plane/test_fabric_join_setup.py` (real CLI, real fabric listener).
- Codex: its sandbox blocked the tailnet listener ("control plane is unreachable"). `summary install` now appends one execpolicy rule, `prefix_rule(pattern=["herdr-eng", "rooms", "join-fabric"], decision="allow")`, to `~/.codex/rules/default.rules` (only when `~/.codex` exists; idempotent, manifest tracked, removed by `summary uninstall`, shown by `--check`). No `network_access`, `sandbox_mode` or `approval_policy` change. Verified with `codex execpolicy check` and a real `codex exec` join, and a no-rule control that fails.
- OpenCode and Pi: not a code defect. Their configured default model (`qwen3.8-27b-...-q8_k_xl`, also OpenCode's `small_model` title model) has no worker on the gateway (503 `capacity_unavailable`), so runs hung or printed the "retry after the declared delay" error. Documented the workaround in `docs/operations/fabric-trust.md` section 9a; both joined for real with a served model.

### Dashboard a11y ratchet lowered

- `ui-gates-dashboard-a11y.test.mjs`: the recorded `focus-visible` findings on the assistant surfaces are removed. They were cleared by the `.hd-btn` fallback focus ring in #723, and the ratchet fails when a finding disappears without the table being lowered.

### Report Evidence and Downloads tables are readable at phone width

- Below 600px (screen only) each row of the Evidence and Downloads tables becomes a labelled block, so no column is cut off and nothing scrolls sideways inside the table. Desktop keeps the real table; print keeps it too, with its repeating header; `#evidence-...` anchors still work. Applies to the public share viewer and the dashboard preview (same renderer).
- Labels come from the column position in CSS, because the sanitiser keeps no `data-*` attributes and its allowlist is unchanged. The table carries explicit ARIA roles (and keeps its header row, visually hidden) because browsers drop table semantics once `display` changes. The roles and classes are only emitted with the full stylesheet, so the 4 KiB minimum-cap envelope is unchanged.
- `report-how-to-read`, `report-anchors` and `report-print` Playwright tests now honour `BROWSERS` (default `chromium,firefox`; a missing engine is skipped). `page.pdf()` checks stay Chromium-only. New `report-narrow-tables` test.

### `/herdr::join`: one skill to join the workspace room from any harness
- New skill `herdr-join` (`herdr_engineering/skill_data/herdr-join`, linked at `skills/herdr-join`). It reads `HERDR_WORKSPACE_ID`, `HERDR_TAB_ID` and `HERDR_PANE_ID`, runs the existing `herdr-eng rooms join-fabric NAME --json` (tokenless tailnet fabric trust; `--no-workspace` opts out), reports agent id, room and the fixed `read`/`post`/`instruct`/`execute` grant, and says plainly that `coordinate` and `grant_agent` are owner-only.
- `herdr-eng summary install|verify|uninstall` now manages it with `summary` and `handoff` (`--skill herdr-join`): a symlink into Claude Code, Codex, Pi and OpenCode skill directories plus an OpenCode `/herdr-join` command. Nothing pre-existing is overwritten; a collision is reported as `preserved`. No top-level `/join` alias and no Pi/Codex prompt are written.
- No session-start auto-join hook: that stays an owner decision. The router entry (`join` in the `herdr` skill's `commands.json`) lives in the separate herdr-handoff repo.
- Tests: `tests/test_herdr_join_skill.py`, `tests/control_plane/test_collab_fabric_join_skill_e2e.py` (real CLI against the real fabric listener).

### Inspector journal skips tool update rows that have an end row, and repeated all-zero usage rows
- A tool call journaled a row per update (each repeating the output so far) plus start and end; 99.8% of `inspector.tool` update rows belonged to calls with an end row. Updates are now held back (latest only) and dropped when the end row carries the output and everything the update added (first valid tool and args_preview, sensitive/redacted/truncated); otherwise the held update is journaled just before the end row, and also when the session is interrupted, ended or restarted, or the call leaves the ring. An all-zero `inspector.usage` report that equals the session's latest report is not journaled (20% of rows); a zero report that would replace a different latest is. `control_plane.journal_suppression.inspector_tool_updates` and `inspector_zero_usage` (default true) turn each off.
- Known replay difference: if the process dies with a call still running and nothing closes it, that call's last update is lost (its start replays). A replayed ring has fewer events, so `rev`, `seen` and sequence numbers differ; tool status/output and `usage` totals/latest are equal. Rows already journaled replay as before. Tests in `tests/control_plane/test_journal_suppression.py` (real PostgreSQL, suppressed vs unsuppressed log).

### SessionUpdated rows that only change `runtime` or `transcript` are no longer journaled
- 98.6% of `SessionUpdated` rows differed from the previous one only by `runtime` (cpu/rss counters) or `transcript` (the reporter's status flips between `identity_unavailable` and `recording`; the cause lies outside this repository). With a durable sink such a write is skipped unless `control_plane.journal_suppression.session_heartbeat_seconds` (default 300, max 600) have passed since the last journaled row, so a replayed `updated_at`, runtime and transcript are at most that old. `session_noop: false` restores journaling every write. The in-memory state, the live SSE stream and every API answer are unchanged; all keys are validated and unknown keys are rejected.
- Product-visible changes: the Activity feed and the durable session history no longer show a `SessionUpdated` entry for each runtime/transcript-only report (they still show every material change and one entry per heartbeat); the per-session event count shrinks accordingly. Tool-update and zero-usage rows (above) disappear from the journal, not from the inspector view.

### Fabric compaction treats ServiceUpdated rows that differ only by heartbeat timestamps as repeats
- `ServiceUpdated` compaction compared payloads exactly, but every heartbeat restamps `metadata_updated_at`, `verified_origin.verified_at` and `metadata_observation.observed_at`, so it removed 32 of 235,252 recent rows while 99.6% differed only by those. Equality now ignores the three timestamps (`control_plane.fabric_compaction.normalize_service_timestamps`, default true; false restores exact equality). The service's first and last `ServiceUpdated` row are still kept, so a replay (including `updated_at`) is identical; rows already journaled replay as before. Tests in `tests/control_plane/test_compaction_ignores_heartbeat_timestamps.py` (real PostgreSQL).

### Single-operator phone account acts as the room owner with no `collab_user`
- In `single_operator` mode a YAML bridge account with role `operator` now acts as `operator_user` for rooms, groups and agent bots, so the phone app lists fabric agents and workspace rooms without a hand-written `collab_user` link. An explicit `collab_user` still wins. Cognito-derived accounts, other roles, replaced accounts and servers that are not single-operator stay unlinked. Tests in `tests/control_plane/test_rakazo_rooms.py`.

### Fabric compaction retries a deferred projection checkpoint on every tick
- With `fabric_compaction.publish_projection_checkpoint`, a publish deferred by the materializer lock (`projection checkpoint deferred (CheckpointInvalid)`) was only retried on the next drained compaction pass, possibly a day later. The compactor now retries it on every worker tick, bypassing `interval_seconds`, with no compaction work and the unchanged `projection_lock_wait_seconds`; the pending flag clears only on a successful publish. Tests in `tests/control_plane/test_compaction_publishes_projection_checkpoint.py` (real PostgreSQL).

### Guest level: Direct is no longer labelled "for now"
- The team-room guest-level card calls the level plain "Direct" and says it acts as Suggest only when Direct is switched off on the service (`team_rooms.direct_enabled`). Tests in `packages/collab-team-rooms.test.js` updated.

### Report previews open with a "How to read this report" legend

- A collapsed-by-default `<details>` under the report title explains only what that report shows: findings, the question/evidence/figure tags, limits, next checks, a figure's reading guide, downloads and the `#` permalinks. Every row is derived from what the renderer emits. The descriptor has no severity or confidence field, so the legend says "Severity, confidence: Not used" instead of inventing a scale. The text is static (never copied from the report) and the summary is a native, keyboard-operable control with a visible focus ring.
- Hidden in print (it is a screen reading aid) and left out of previews under the 16 KiB budget.

### Report sections and findings have stable, linkable anchors

- Every section heading, finding and figure in a report preview now has a stable id (`sec-<section>`, `finding-<id>`, `figure-<id>`, derived from the descriptor id, never the title) and a visible `#` permalink in its heading: a 24px target, always visible, keyboard-focusable with a visible focus ring, accessible name "Link to this finding" described by the heading. No free text reaches an attribute (the share sanitiser aborts a render whose attribute looks like a script), so a finding titled `onerror=` still renders. Previews under the 16 KiB budget stay plain (no permalinks).
- Opening a URL with `#finding-f1` scrolls to and highlights the target on the public share page (CSS `:target`, no script). In the dashboard preview (a shadow root, where `:target` cannot work, and where the server drops relative hrefs and the report stylesheet) `artifact-static-preview.js` rebuilds each permalink as a real link, scrolls and highlights on click and moves focus to the target; `#/artifacts/<id>?version=<v>&at=<anchor>` opens the preview and reveals that section.
- The sanitiser allowlist is unchanged. Tests: `tests/control_plane/test_artifact_report_render.py`, `packages/artifact-static-preview.test.js`, `tests/playwright/report-anchors.test.mjs` (light/dark, desktop/390px, keyboard, hostile title).

### Share panel and share viewer: accessibility sweep

- Fix: the dashboard share panel's Links/Collaborators/Activity/Feedback tabs now follow the ARIA tabs pattern: roving `tabindex` (one tab stop), Arrow/Home/End move and select, per-mount ids with `aria-controls` on tabs and `aria-labelledby` on panels. They use the design system's `.hd-tabs`, so the selected tab is visibly distinct (all four were identical filled buttons).
- Fix: `.hd-btn` lost its focus ring outside `.cx-page`/`.cx-embed` (`collab.css` set `outline:none` plus an undefined `--cx-ring`), so Review, Events, Revoke etc. had no focus indicator on the artifact page; the ring variable now has a fallback.
- Fix: the share viewer's Rich view toggle had no visible focus indicator in Firefox and WebKit (white outline on the page background); it has an explicit `:focus-visible` outline, and `aria-controls` names the region it shows.
- `tests/playwright/share-a11y.test.mjs` (chromium/firefox/webkit): tabs keyboard behaviour, focus indicators, static contrast/name checks in light and dark, no horizontal scroll at 390px, optional screenshots via `SHOT_DIR`.

### Share panel polish

- The Version and Audience labels now stack their select with a 4px gap, the sharing tab buttons are separated by a 6px gap (and wrap), and the author-HTML checkbox glyph is 18px instead of the browser's 13px.

### Reports print and save as PDF cleanly

- The report stylesheet (`artifact_report_render.py`, used by the dashboard preview and the public share page) has a full `@media print` section: paper is always light and black-on-white whatever the screen theme or forced `data-theme`; the contents pills, download buttons and the opt-in Rich view panel are hidden; findings, figures, callouts, notices and table rows are never split and headings stay with what follows; a figure taller than a page shrinks to fit (180mm); tables lose their scroll box and repeat their header row on each page; the closed "Method and configuration" details prints open; an `http(s)` link spells out its URL. The tight-budget core stylesheet keeps the light override too.
- `packages/artifact-static-preview.js` adds a one-off print rule so the dashboard's 640px scroll box does not clip a printed report. Printing the dashboard page itself still includes the app chrome.
- `tests/playwright/report-print.test.mjs` emulates print in chromium (light and dark, OS-driven and forced) and checks `page.pdf()` text: page count, no clipped content, no finding or figure card split across pages. Unit tests: `tests/control_plane/test_artifact_report_render.py`, `packages/artifact-static-preview.test.js`. `_FULL_CSS_MARGIN` rose 1200 to 2000 because the stylesheet grew.

### Compaction publishes a projection checkpoint

- `control_plane.fabric_compaction.publish_projection_checkpoint` (default off) makes a drained compaction pass that moved rows publish a fresh projection checkpoint, so web boot does not replay the journal after a backlog drain exceeds the incremental budget. `projection_lock_wait_seconds` (default 60) bounds the wait for the materializer lock; a busy lock or failed publish logs a warning and retries on the next drained pass. The compaction lock is released before the publish.

### Rich view: cross-engine and phone-width verification

- `tests/playwright/share-rich-view.test.mjs` now runs per engine (`BROWSERS`, default chromium,firefox; webkit opt-in with the software-Mesa workaround) and adds 390x844 light/dark checks (no horizontal scroll, 24x24 target, tap and keyboard) for the public share toggle and frame.
- `tests/playwright/share-author-html.test.mjs` gains the same phone checks for the dashboard share panel's "Include the author's HTML page" checkbox, and webkit support.
- Fix: that checkbox's label was 22.5px tall (under WCAG 2.2 AA 24x24 target size); it is now a 32px-tall flex row.

### CI: a full run no longer dies without a verdict (#651)
- **Root cause, reproduced.** A complete `pytest` run leaks roughly **3,000 file descriptors** — measured on Linux at `df39b736` across 20,033 tests: 15 open at the first test, 3,032 at the last, climbing monotonically. On a host with the common **1,024** soft limit the suite runs out about a third of the way through and pytest dies with `INTERNALERROR ... OSError: [Errno 24] Too many open files` — a truncated log, **no summary line, no verdict**. That is the summary-less death #651 described, and it is why the same tree looked like a hang: the run stopped at 38% mid-progress-line and left nothing to read. Causal proof, same tree both ways: `ulimit -n 1024` → died at 38% with INTERNALERROR; `ulimit -n 65536` → `4 failed, 19928 passed, 99 skipped … in 2:13:44`, i.e. a real verdict.
- `scripts/ci/ensure-fd-limit.sh` raises the soft limit to the hard limit before the suite starts, and **refuses to run** if it still cannot reach a 16,384 floor — naming what would otherwise happen (`Too many open files`) and the fix (`ulimit -n`, `LimitNOFILE=`). A run that stops with an explanation is a result; a run that stops silently at 38% is not. `scripts/ci/unit-gates.sh` calls it before pytest and exits on refusal.
- `pytest-timeout` is now a dev extra with `timeout = 900` / `timeout_method = "signal"`. The suite also stalls for 6–12 minutes at a time on a loaded host (observed: 54% for ~6 min, 84% for ~12 min, both recovering), and a stall that never ends costs the whole pipeline and names nothing. 900s is a hang detector, not a performance budget, and `signal` is set explicitly so one hung test reports and the run **continues to a summary** rather than the process being killed. Verified against a deliberate 30s stall at `--timeout=5`: `FAILED … test_a_stall_names_itself - Failed: Timeout (>5.0s)` followed by `1 failed, 1 passed`.
- `tests/test_ci_fd_limit.py` (10 tests) drives the real shell function in subshells — raised, left alone, and refused-with-a-message — plus guards that the gate calls it *before* pytest and that the timeout stays declared. The three hard-limit cases skip on macOS, where a process may not lower its own hard limit; they run on Linux.
- **Not claimed:** that CI's `killed` pipelines were caused by this. No CI log in the last 25 runs mentions `Too many open files`, so the agent's limit is already adequate; those kills split into superseded pushes (8 and 27 min) and runs that simply outlasted the window under load (68 and 79 min — this suite took 2h13m on a host running six concurrent suites). The FD fix removes a failure mode that was silently fatal on any host with a normal default, which is what #651 was. The leak itself is still there and still measured; it is now survivable and named rather than invisible.

### CI: a push to `main` is now gated (#691)
- `.woodpecker/structural-gates.yml` is the first pipeline that triggers on `event: push, branch: main`. `unit-gates.yml` and `ui-gates.yml` trigger on `pull_request` and `manual` only — deliberately, so a push to a PR branch does not build twice — but the same filter also excluded `main`, so a commit that broke `main` was never detected by `main`. It surfaced as a red pipeline on the next unrelated PR that rebased onto it, which cost two misdirected investigations on 2026-11-05/06.
- It is not a cheaper `unit-gates`. `main` takes ~85 commits a day (251 on the busiest recent day) and `unit-gates` takes ~55 minutes, so gating every push to `main` with it would need ~78 runner-hours a day and starve PR CI. The new gate answers the questions whose failures actually reached `main` — every tracked YAML/JSON/TOML file parses, no tracked file carries an unresolved conflict marker, and `memory_install.PINNED_REVISION` equals the lock — in **1.34 seconds** over 2858 tracked files, so ~85 runs a day cost about two minutes of runner time. The full suite still runs on every pull request.
- `scripts/ci/structural_gates.py` (the checks), `scripts/ci/structural-gates.sh` (the runner, reproduces CI locally), `tests/test_structural_gates.py` (15 tests). Mutation-checked against the real incidents: reintroducing the exact ` origin/main` line `a4a1fe45` shipped exits 1 on the parse check; a one-sided pin bump reports both revisions by name; an unresolved marker is found. Two checks were corrected during prototyping because they were wrong about the repo, not about the incidents — `revision: null` is a deliberate unpinned dependency (7 of 12 use it), and a bare-ref grep false-positives on ordinary prose and shell, so that check was dropped in favour of the parse check, which catches the real case exactly.
- Opened as a reviewable step toward #691; the remaining option there — requiring a PR to be green against current `main` at merge time — is a branch-protection setting, not a repo change.

### Approval-gated external actions: a GitHub issue for a team and a Gmail draft reply (unified workspace, slice 5; off by default)
- New `control_plane.personal_assistant.external_actions` block (YAML only; `enabled`, `kill_switch`, `actions_per_hour`, `executions_per_hour`, `intent_stale_seconds`, `github.api_base|timeout_seconds|labels`; also needs `personal_assistant` with persistence and `team_rooms`). Two actions, both built from an email brief the operator already approved: `github_issue_create` (in the repository the room owner sets per team with `herdr-eng rooms issue-target`) and `email_reply_draft` (a Gmail DRAFT; nothing in the code sends). The room owner's issue writer, coordinator or the owner propose; ONLY the account owner approves, rejects or retries. Everyone else gets 403 and an audit record under their own identity; strangers get 404.
- Exactly once, with stated limits: the key is a SHA-256 of kind and brief key; the intent is written durably before the provider is called; every later run looks for the key at the provider first (issue body marker, draft header) and adopts what it finds. A lost answer, crash or restart leaves the action visibly `unknown` and only the owner's retry (re-confirming the content hash) runs it again. A rare second issue or draft is possible when the provider answers late (threat model R-EA1). Approving needs the SHA-256 of the content shown, so what is approved is what is sent.
- Credentials: the GitHub App's own `team_issues` write capability, else the existing `github.token` (documented fallback); the Gmail draft goes through the person's existing Google connection. No environment variable, no secret in any response, log or audit record (swept in tests).
- New routes `/api/v1/assistant-external-actions[/<id>[/decision|/retry]]` (operator API key), collab `GET|POST /rooms/<id>/teams/<team>/issue-target` and `/rooms/<id>/external-actions[/<id>/decide|/retry]`, capability `assistant_external_actions`, and `herdr-eng rooms issue-target|external-list|external-file|external-decide|external-retry` and `herdr-eng assistant external-list|external-show|external-file|external-decide|external-retry`. No migration. Flag off changes nothing (pinned by a test).
- Tested with a real Postgres, collab service and HTTP, but against CONTRACT-LEVEL FAKE GitHub and Gmail servers: neither real service was ever called. Not built: web UI, the attention inbox, calendar actions, any send. Roll back the flag, not the image (R-EA4). See `docs/operations/team-rooms.md`; threat model section 3.9.

### Memory client pin: 0.1.7 → 0.1.8
- `PINNED_REVISION`/`PINNED_CLIENT_VERSION` and `lock/upstreams.yaml` move to `d48b334b6236065aa188a696dc6e9095b04ca761` (client `0.1.8`, contract v1 unchanged). One commit ahead of the previous pin: `fix(client): name the row that stops an outbox from draining` (herdr-memory#16). Fleet hosts were already running 0.1.8 from upstream `main`, so `memory doctor` reported `package_revision`/`client_version` drift on every host; this reconciles the pin with what is deployed rather than pulling anything new in. Verified against the upstream tree at that revision (`pyproject.toml` → `version = "0.1.8"`), not from a tag or a version string in a log. `tests/test_memory_install.py::test_pin_matches_lock` fails on a partial bump.
- Also drops the stray blank line left in `lock/upstreams.yaml` between `maintenance:` and `note:` — the scar of the invalid ` origin/main` line that `a4a1fe45` shipped and #692 documents.

### Share panel: include the author's HTML page
- The dashboard Share card now has an unchecked "Include the author's HTML page (Rich view)" box with the caution "Anyone with the link can view this page. It is shown without scripts." It appears only for a public share of a report version with exactly one HTML asset (same choice as `pick_html_asset`, read from the version's report descriptor; no new endpoint). Checked sends `include_author_html: true`; unchecked sends no such field.
- A pending share that includes the page lists `author-page.html` and the server's warning, shows an "Includes author page" badge, and no longer exposes the internal entry `preview.html+author-html` (it shows `preview.html`).
- Tests: `artifact-share-panel.test.js` (+9), real Chromium and Firefox `tests/playwright/share-author-html.test.mjs`.

### Drift graph layout router
- The graph no longer falls into one tall column that leaves half the pane empty: boxes are packed into balanced rows (label width 40 down to 8 tried) joined by arrows, with an elbow connector from the end of a row to the start of the next, and the arrangement with the fewest lines wins. A single column remains for panes too narrow for two boxes; the drift marker stays under its own box, including at the end of a row. ASCII and the width guarantee are unchanged.

### Report share Rich view (opt-in)
- A public share link of a report can include the author's own single HTML page, only on an explicit `include_author_html` (CLI `shares create --include-author-html`; default off; refused for member-created links). The page is picked by the dashboard's `pick_html_asset`, capped at 2 MiB and SHA-256-verified; the approval plan lists `author-page.html` (name and size, `author_html: true`) with a warning that anyone holding the link can view it. Without the opt-in the bundle never contains it and the route answers 404. Approval and revocation work as before; no migration (the flag rides a sentinel in the free-text `entry` column until approval writes `rich` into `state.json`).
- The share host serves `/s/<token>/f/author-page.html` with the dashboard Rich view headers minus the two framing controls (bare `sandbox` CSP with no `allow-*`, nosniff, no-referrer, no-store, noindex; no `frame-ancestors` or `X-Frame-Options`, which an opaque-origin parent can never satisfy; the dashboard route is unchanged), with the preview's access, revocation and expiry checks. The preview of an opted-in share gets a "Rich view" toggle that mounts `<iframe sandbox="">` ("Scripts are disabled in this view") The preview keeps its opaque origin (no `allow-same-origin`); only its meta CSP gains `script-src 'sha256-...'; frame-src 'self'`.
- `test_no_frames.py` allow-lists `share_static/rich-toggle.js` with a pinned shape; variations are asserted rejected. Tests: `test_share_rich_view.py` (21), real-Chromium `tests/playwright/share-rich-view.test.mjs` (4).

### Report preview hides sections the byte budget leaves empty
- When a section's share of the byte cap fits none of its entries (for example Evidence at a 12000-byte cap), the preview no longer shows a bare heading and table header: the section and its Contents chip are left out. The omission note still appears and keeps the counts ("Shown: 0 of 3 evidence entries."). Sections that are empty in the descriptor are unchanged (still absent).

### Build cache backend follow-ups (all default off)
- `Registry.reconcile` now runs at build start and in the cleanup sweep (expired leases with dead owners only).
- New contract events `cache_stats_updated`, `storage_degraded`, `lease_changed`, `cleanup_completed` (`contract-events.jsonl`), published on the build-cache stream.
- `GET /api/v1/build-cache/events/stream` with SSE `id:` and Last-Event-ID resume.
- Opt-in ingest of other hosts' builds: `POST /api/v1/host/build-cache/report`, `control_plane.build_cache.ingest`, bounded in-memory retention, no migration.

### Report figures inline
- Report previews (dashboard, raw preview route and public share links) show each image figure inline as a `data:` `<img>` above its caption (PNG/JPEG/GIF/WebP, and SVG as an opaque image, so its script never runs); other types, oversize images (over about 150k characters) and anything that does not verify keep the caption and download link, and the omission note says so. The sanitiser's data-image allowlist gained `svg+xml`; no tags were added.

### Report Rich view: the author's own HTML page, sandboxed (PR 3 of 3, pretty reports)
- New owner route `GET /api/v1/artifacts/{id}/versions/{vid}/rich` (`artifact_rich_view.py`, 3 lines in `route_render`): serves the report version's single `text/html` asset (when several, the single one with role `preview`; otherwise 404; also 404 for an unknown version or a non-report artifact) with the same read permission as the other versioned render routes. Bytes are size- and SHA-256-verified; above 2 MiB the answer is 413. Headers: `Content-Security-Policy: sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'` (the bare `sandbox` directive: opaque origin, no scripts even if the markup has them, no `allow-*` token), `X-Frame-Options: SAMEORIGIN`, `nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: private, no-store`. `/live` is unchanged and still not frameable.
- Dashboard: a report version with an HTML asset gets a "Rich view" card. The sanitised preview stays the default; the toggle mounts `<iframe sandbox="">` (empty sandbox) pointing at the rich route, with "Open in new tab" (the rich route itself, still script-free) and the line "Scripts are disabled in this view; download the HTML asset to run it". `packages/artifact-rich-view.js` refuses any other `src`.
- The no-frames policy is relaxed narrowly: `test_no_frames.py` allow-lists only `packages/artifact-rich-view.js` and pins the exact frame shape (empty sandbox, guarded rich-route `src`, no `srcdoc`, no `allow-*`, no `postMessage`); ten variations (scripts token, other src, srcdoc, a second frame, an embed, a changed route pattern, ...) are asserted rejected. Spec amended (`2026-10-30-artifact-sharing-gui-and-collaboration-design.md`).
- Public share links: see "Report share Rich view" below (opt-in).
- Tests: `test_artifact_rich_view.py` (11), `artifact-rich-view.test.js`, real-Chromium `tests/playwright/report-rich-view.test.mjs` (author page renders, its script does not run, `self.origin` is `null`, parent access throws `SecurityError`, screenshots light 1100 / dark 390).

### Fabric agent (macOS): an install that left the agent stopped now fails
- The nightly fleet update on 2026-11-05 left the mac's fabric agent **stopped and reported success**. `scripts/fabric-agent-macos.sh` ran `launchctl bootout` (killing the running agent), its `bootstrap` then failed, every launchctl call was `|| true`-swallowed, and the script only `warn`ed before exiting 0. The Ansible caller's `launchctl kickstart -k` was best-effort too (`failed_when: false`), so nothing in the pipeline could see it. The first thing that noticed was the *next* night's parity check — `revision parity failed: invalid_host mac` — roughly 30 hours later, and only because #613 had made that line name the host. The agent is now loaded by `scripts/fabric-agent-load.sh`, which settles the question the only way it can be settled — asking launchd whether the label is loaded — and exits 1 if it is not; `bootstrap`'s own status is deliberately ignored, because launchd answers `5: Input/output error` for a label that is already loaded and treating that as a failure would be the mirror-image bug. The installer task in `ansible/tasks/fabric-agent.yml` has no `failed_when`, so the failure now reaches the play. Split out into its own script so it is testable where launchd does not exist: the unit gate runs the whole suite on Linux, and a macOS-only failure path no test can reach is exactly how this shipped. `tests/test_fabric_agent_macos_load.py` covers it against a stub launchd (8 tests; reverting the fix to warn-and-exit-0 fails exactly the two failure-path tests), plus `test_the_agent_installer_failure_is_not_swallowed`, and the loader was run against real launchd on the affected host both ways. The agent is running again (`agent=active`, parity restored).

### `/handoff` skill and `/summary` visibility
- New `handoff` skill (`/handoff [pi|claude|codex|opencode]`), installed by `herdr-eng summary install` (`--skill summary|handoff|all`) with the same manifest and never-overwrite contract; no Pi prompt template (Pi's built-in `/handoff` keeps its name). Default target is the current harness, from the new `herdr-eng handoff harness` (explicit, ancestor process, pane agent, marker env; exit 2 when undetectable). Needs herdr-handoff 0.2.0 (envelope v2, `--from-json`, pane-less `launch`). Opt-in trigger ideas: `docs/operations/handoff-auto-triggers.md`.
- `summary publish` now returns a `digest`, reuses a still-open report viewer pane (refreshed with SIGWINCH) instead of closing and re-splitting, and focuses the report pane.



### Fleet rollout watcher, phase 3: event model, read endpoints and outbox emitter (off by default)
- Implements step 3 of `docs/specs/herdr-fleet-rollout-watcher-20261104/spec.md`. `POST /api/v1/ingest` accepts `entity_type=fleet_rollout` (`rollout_id` = `entity_id`) through `validate_fleet_rollout`, a strict allow-list per event type. **No free text anywhere** (deviation from spec section 8, decision 2026-11-05, recorded in the spec): every value is a closed enum, a fleet host, lowercase hex, an integer or a UTC timestamp, and unknown fields are rejected, so a secret cannot be stored by construction. `log_tail`, `pr_title` and free-form check names are gone; `checks[]` is `{name (closed enum), status (ok|failed|skipped), duration_ms}` and a log is a host-side reference `{host, path_id (updater|installer|healthcheck), sha256, bytes, lines}`. Regex redaction was built and withdrawn: reviewers kept finding readable secret shapes. Replays are idempotent on `<rollout_id>:<seq>`; a different payload for a stored `seq` is a 409.
- New read routes `GET /api/v1/fleet-rollouts[?limit=&repo=]`, `/fleet-rollouts/{id}` and `/fleet-rollouts/versions` (ETag), derived in memory from the append-only streams (`repo.list_fleet_rollouts`, `get_fleet_rollout`, `fleet_rollout_versions`); no SQL table, no migration.
- New `fleet_watch/events.py` (`Emitter`: validates with the server's validator, assigns `seq`, appends to a private fsynced `outbox.jsonl` before posting, so a control-plane outage loses nothing; permanent rejections, including a 200 that is not ok, are parked in `outbox.rejected.jsonl`; capped at 2000 events with the overflow parked and counted as `dropped`; flush never holds its lock across a network call) and `fleet_watch/logs.py` (log text stays in a 0600 store on the host, last 1 MB, 30 days). `seq` is never reused: max of both counter files and everything on disk, persisted before the append; counters are never pruned; a total counter loss restarts from the clock. New `herdr-eng fleet-watch outbox status|flush` and `fleet-watch log show <rollout_id> --host H`. Nothing emits events yet (the runner is step 4). Docs: `docs/operations/fleet-rollout-events.md`.
- Tests: `tests/control_plane/test_fleet_rollout.py` (validator matrix; every leaf of every event type replaced with 500+ secret-shaped strings from `make_secret_corpus()` in the test module (strings synthesized by shape at test time; no credential-like literal is committed) must be refused, also through real HTTP; projections; reload) and `tests/test_fleet_watch_events.py` (outbox against an unreachable-then-reachable real control plane, a 503 server, seq recovery, caps, the log store, CLI smoke in a throwaway HOME).


### Direct guest level for team rooms (unified workspace; off by default, own flag)
- New `control_plane.collab.team_rooms.direct_enabled` (YAML only, hot, absent = off; also needs `team_rooms.enabled`) plus `direct_default_per_hour` (3) and `direct_default_max_open` (2). With it off the stored level `direct` still acts as `suggest` and a guest has no task right, exactly as before (a test pins both); the guest-level view now reports `behaves_as` from the flag and the effective limits.
- With it on, a guest of a room at `direct` hands work to an agent through the ordinary `POST /rooms/{room}/tasks` (`target_agent_id`, or `target_role` routed to the least-loaded holder) inside the owner's `direct_limits`: an agent or role allow-list, `per_hour`, `max_open`, `max_text`, and the off-by-default `may_request_review` and `may_cancel_own`. The task is the guest's (their user id on every event), `origin: guest-direct`, tainted `external-source:guest`, with a fixed-sentence command and the guest's words as quoted untrusted data, like an approved Suggest request. Caps are counted from the replayed log; `TASK_RATE` and the per-agent open cap still apply.
- Refused with 403 plus an audit event (or 429 for caps): targets outside the allow-list, personal assistants, coordinator agents and external agents, a named person, parent, lease, policy or team, a chosen reviewer, a review request without the flag. An agent cannot create a task under a guest's task (400 `direct_no_children`), so there is no depth to set; nothing is turned into a Suggest request automatically.
- Revocation cancels, never requeues: lowering or narrowing the level, removing the guest, withdrawing one guest's right, the guest expiring and the flag turning off make guest tasks unservable at once and the sweep (`sweep_direct`) writes `cancelled` with reason `direct_revoked`; a late update from the old run is a 404. Lowering the level and withdrawing a right are safety writes (work with a full log); setting `direct` is pre-paid.
- New routes `GET /rooms/{room}/direct-tasks`, `POST .../direct-tasks/{id}/cancel`, `GET|POST .../direct-guests` and `herdr-eng rooms direct|direct-tasks|direct-cancel|direct-guests` (plus `guest-level --may-request-review --may-cancel-own`). Threat model rows R-DG1 to R-DG6 and `docs/operations/team-rooms.md` (operation and rollback: the flag is the rollback; roll the image back only after removing the key). Web UI not touched: the label in `packages/collab-team-rooms.js` (PR #598) says Direct "acts as Suggest for now" and must change when both land. Not deployed.
 origin/main

### Memory install: Claude lifecycle hooks no longer fail on a host without `herdr-eng`
- `herdr-eng memory install/repair` now writes the Claude `SessionStart` and `UserPromptSubmit` hooks as `[ -x "<exe>" ] && "<exe>" memory hook claude <Event> || true # herdr-memory:managed` (absolute path, valid in `/bin/sh`, no PATH dependence), so a synced settings.json is a silent no-op where the binary is absent instead of failing every session. Entries written by earlier versions carry the same marker, are reported stale, and are upgraded in place on the next install/repair (no duplicates, user hooks untouched); a repair on the guarded form is a no-op.
- Tests (`tests/test_memory_install.py`) cover install, in-place upgrade, no-op repair, status, and run the command line under `/bin/sh` with and without the binary (throwaway HOME).

### Fleet rollout watcher, phase 2: read-only `fleet-watch once --dry-run` (off by default)
- New package `herdr_engineering/fleet_watch/` implementing step 2 of `docs/specs/herdr-fleet-rollout-watcher-20261104/spec.md`: a poller that does a conditional GET of `branches/main` per watched repo (stored ETag, `304` is free, 120 s +-20 s jitter, 10 min when `x-ratelimit-remaining < 100`; `Retry-After`, then `x-ratelimit-reset`, then 2 -> 15 min exponential backoff on 403/429/network errors), the D3 published-SHA verifier (the SHA must be the API head and `merge-base --is-ancestor` of `origin/main` in the watcher's own bare cache clone; stray unpushed and force-pushed-away commits are refused), a private flock-guarded state file (`$XDG_STATE_HOME/herdr-engineering/fleet-watch/state.json`), and the coalesced rollout planner (`rollout_id` = sha256 of sorted `repo:sha`, hosts `beast -> bender -> macbook-pro?`).
- New command `herdr-eng fleet-watch once --dry-run [--repo OWNER/NAME] [--lock PATH] [--json]` prints the planned rollout. It makes no ssh call, emits no event, and never writes `fleet.lock.yaml` (it only reads `repos:` as the baseline); without `--dry-run` it exits 2. Only the poll cache (ETag, head, backoff) is written. No systemd unit yet. Test overrides: `HERDR_FLEET_WATCH_API`, `HERDR_FLEET_WATCH_GIT_BASE`.
- Hardening: state shapes are validated (wrong shapes are set aside), http.client errors degrade a poll, 0700/0600 enforced on existing paths, an API head is required and must equal the fetched origin/main tip, backoff has a 5 s floor, and no partial rollout is planned while any watched repo is held (backoff, error, refused).
- Tests (`tests/test_fleet_watch.py`) use real git repos and a real local HTTP server speaking the branch endpoint with ETag/304/403/429; the CLI smoke runs in a subprocess with a throwaway HOME.

### Fleet updates: the controller runs from a checkout nothing else touches
- The nightly run skipped again after the checkout was cleaned: another session had added a 9-line routing shim to `deploy/control-plane/deploy.sh` at 11:45, fourteen minutes before the 11:59 timer. The skip was right — the work was preserved — but unattended fleet updates are now one stray edit away from silently never running, and the only signal is a `WARN` from a doctor nobody runs on a schedule. The timer now runs from a dedicated clone: non-editable install, private inventory copied, `WorkingDirectory=%h`, `runner_is_checkout: false` recorded, and the next run reported `outcome: updated`. Steps are in `docs/operations/fleet-lifecycle.md`; D32 in the drill report.

### Memory client pin: 0.1.7 (offline config validation)
- `herdr-eng memory doctor` on a host running the current client reported `client 0.1.7 != pinned 0.1.6; promote the pinned build` — the pin, not the host, was behind. Promoted to `7d8499af11d3` (herdr-memory#15: `validate_config()` runs without network or runtime state, so a bad config is a local error rather than a probe failure). 0.1.7's own suite is green (166 passed, 4 skipped, 20 subtests). `lock/upstreams.yaml` moves with it; `test_pin_matches_lock` fails if they drift.

### Fleet status: the macOS fabric-agent probe asked launchd for the wrong service
- `herdr-eng fleet status` and every `herdr-eng fleet update` reported `agent=inactive` for mac, `_parity()` put the host in `invalid_hosts`, and the unattended nightly run exited 1 on a fleet that was healthy. The probe in `ansible/tasks/fleet-fingerprint.yml` asks `launchctl print gui/<uid>/herdr-eng-fabric-agent` — the *systemd* unit name — while `scripts/fabric-agent-macos.sh` has registered the LaunchAgent as `com.herdr-engineering.fabric-agent` since the installer was written (4bc6b541). `launchctl` finds nothing, the probe answers `inactive`, and no mac has ever passed the check since the probe landed (57615222). The probe now asks for the label the installer writes; `test_fleet_fingerprint_probes_the_service_names_the_installers_actually_use` reads the label out of the installer and the unit name out of the Linux installer, so neither platform's name can drift from the probe again. Verified on the host that was failing: the probe snippet returns `active` for the running agent (pid 26639).

### The assistant offers a team a derived brief (unified workspace, slice 4; off by default)
- New `control_plane.personal_assistant.team_briefs` block (YAML only; `enabled`, `topics`, `known_senders`, per-hour caps, `summary_max_chars`; also needs `personal_assistant` with persistence and `team_rooms`). The operator's assistant derives a brief from named fields of one email (redacted, capped, addresses and links masked, injection flag) and files it as a pending Suggest request with the taint `external-source:email`; the raw message, headers and addresses never enter collab, the room log, the Work Graph, events or audit. An opaque `bref-` reference lives only in the assistant store and 404s for everyone but its owner.
- No setting skips the operator's approval (unknown keys stop startup). Approval makes a NEW operator-origin task with the brief as quoted data; the taint stays. The approval card shows exactly what the team and an opted-in external coordinator will see, and the owner can trim it. The same email is one request across restarts and crashes (key is a SHA-256 of user, provider and message id); a second room or team is 409.
- New routes `POST/GET /api/v1/assistant-team-briefs` and `GET /api/v1/assistant-references/<id>`, and `herdr-eng assistant brief-offer|brief-list|brief-reference`; `rooms queue` shows the brief card. No migration. Flag off changes nothing (pinned). Not built: web UI, automatic inbox scanning, external actions, `direct`. Roll back the flag, not the image (references are lost on an image rollback). See `docs/operations/team-rooms.md`; threat model section 3.8.

### Fix: the fabric agent forwarder no longer breaks SigV4-signed requests
- `AgentForwarder` rewrote `Host:` to `127.0.0.1:<port>`, which invalidates AWS SigV4 (the signature covers Host), so rustfs/MinIO answered `SignatureDoesNotMatch` on the tailnet address. SigV4-signed requests (Authorization header or presigned query) are now forwarded byte-for-byte; unsigned requests behave as before. Takes effect when the operator restarts the agent (`systemctl --user restart herdr-eng-fabric-agent.service`). Proven with a throwaway forwarder on the tailnet IP: aws-cli list/put/get/delete and sccache cold+warm via `http://100.64.8.6:<port>`.

### Fabric build cache: the shared tier works against the herdr rustfs server
- `herdr-eng build-cache-rustfs-provision` (dry-run default, `--apply`): additive, idempotent creation of bucket `herdr-build-cache` (hard quota), read/write and read-only users with bucket-limited policies, and 0600 credential/registry files. Admin credential from the central fleet config, never echoed; MinIO client runs in the container with a private removed config dir; never touches `herdr-artifacts`; no delete path.
- `build-coordinate` now starts or reuses a per-policy sccache server before cargo with the credentials in the SERVER environment only (cargo/rustc/build scripts get just the port), `close_fds=True`, idle-timeout reaping. `cache.shared.active` is true only after the server is verified (signed list request + `--show-stats` bucket/prefix). New reasons: `server_start_failed`, `server_not_shared`, `credentials_rejected`. `shared_requires_server` is now only the transient validated state.
- Plain HTTP is accepted for loopback and tailnet hosts (127/8, `::1`, 100.64.0.0/10, `*.ts.net`, MagicDNS names); other hosts still need TLS. Config key names keep their S3 spelling for compatibility; user-visible text says rustfs.
- Fix: the generated sccache TOML was rejected by sccache 0.19.0 (`[cache.s3]` needs `no_credentials = false`). `sccache --show-stats` for a build event now queries the policy server (child env) instead of the default port, and the delta includes per-level `multi_level` counters.
- Measured with real sccache 0.19.0 and the real local rustfs: cold build writes to the namespace prefix, a clean build with a new local cache hits from rustfs, another namespace misses, the read-only user cannot write. Not measured: a real second host; the tailnet address (the fabric agent forwarder rejects valid SigV4 requests).

### Installer: checksum-pinned sccache 0.19.0 for the fabric build cache
- `scripts/install.sh` step 2b runs `scripts/install-sccache.sh`: downloads the upstream mozilla/sccache 0.19.0 tarball for this os/arch (Linux x86_64/aarch64 musl, macOS arm64/x86_64), verifies SHA-256 against `scripts/sccache-checksums.txt` (fails closed if absent or mismatched), installs only the binary to `~/.local/bin/sccache` (0755, no root), skips when >= 0.19.0 is present. Failure is non-fatal with a loud warning. `--no-build-cache-tools` skips, `--build-cache-tools-only` runs just this step. `herdr-eng doctor` gains `build_cache_sccache`, `build_cache_rust`, `build_cache_python_deps` (read-only warnings with the fix command). The feature stays default-off.
 origin/main

### CI: the ui-gates focus-audit floor is back at 480
- Commit 643d2b5f lowered the owner `focused` floor in `ui-gates-states.test.mjs` from 480 to 470, blaming the Usage page's new tabs. The Usage page is not audited by that test (every state's scope is a workbench panel, `#hcn-p-*`); the real drift was the phone's closed Filters disclosure, which 8cbeff9b fixed by opening it in every lineage state (498 focused before the drift, 479 during). The floor is 480 again and the stale comment is replaced. `ui-gates-floor.test.mjs` reads the floors from source and fails if any drops below its recorded minimum (runs without a browser). Not verified locally: Playwright is not installed here, so only a green ui-gates run proves the owner total reaches 480; do not merge before it.

### Summary: the drift graph shows time worked and tokens per task
- Each graph box now shows how long was actually worked on that segment (pauses over 30 minutes are not counted; the wall-clock time is always shown beside it, in each box, the total and the drift cost) and the tokens used, fresh (new input + output) over cached re-reads, e.g. `42m worked · 3h wall` and `180k fresh · 3.1M cached`. A total line closes the graph, and the drift marker states what everything after the topic change cost. Claude, Pi, Codex and OpenCode all work: `session_history.py` reads each harness's own usage records (Codex's per-turn figure, never its running total; a Claude message repeated across content blocks counted once). Computed by code from the history, never by the model. A figure that was not recorded is left out rather than shown as 0; child-agent usage kept in separate records is not counted and the total says "main session only".

### Summary: a drift graph under the summary pane, and a viewer that reflows
- `/summary` can now show where a session drifted: a graph of topic segments (start time and prompt count each, an agent badge where the history reports child agents) with a marker where the topic changed into something that deserved its own session, in a pane directly under the summary. The summarizing model proposes labelled segments in an optional `drift-graph` block; `publish` checks them against the real prompts and computes every time and count itself, so the model cannot invent a number, and any block that does not check out is dropped without touching the report. Harness-neutral: it uses only the normalized history (`session_history.py` now returns every real prompt with its time, plus the true total, instead of losing the middle of long sessions to the 400-event trim), so Claude, Pi, Codex and OpenCode behave alike. Drawn with line characters (`graph_render.py`: a row of boxes, a column when narrow, a numbered list under 30 columns, ASCII outside UTF-8, `NO_COLOR` honoured).
- Both panes use a small full-screen viewer (`pane_viewer.py`) instead of `less`, so a resize re-wraps the text at the new width and keeps the reader's place; before, a resized pane kept the old wrapping. Closing either pane closes both (`--peer`), including on Ctrl-C or an error. New: `herdr-eng summary graph`, `summary view --peer`, and `scripts/check_summary_panes.sh`. Old reports have no graph.

### Summary: the side pane renders the report, and closes when you quit it
- `/summary` opened the report in the pane with `less -R` on the raw Markdown, so it showed `#`, `**` and backticks, and the emptied shell pane stayed open after you left the pager. The pane now runs `herdr-eng summary view`, which renders the report with a dependency-free Markdown renderer (`md_render.py`: coloured headings with rules, bold, italic, inline code, nested lists, fenced code, quotes, tables, wrapping to the pane width; `NO_COLOR` is honoured), pages it with `less`, and closes its own pane in a `finally` when the pager exits, so quitting, Ctrl-C or a pager failure all leave no pane behind. A report whose pane is still open is replaced rather than typed into, because keystrokes sent to a running pager are pager commands. The pane runs the viewer with the interpreter that published the report, so an older `herdr-eng` on PATH cannot be picked up. Tests cover the renderer, the viewer's close in every ending, and the pane replacement; disabling the close makes three of them fail. Verified by hand in a Herdr pane: rendered screen, then `q` closed the pane.

### Fleet status: a parity failure now says which host failed
- `herdr-eng memory doctor` printed `unattended update failed: None`. The nightly updater records `report.get("error")`, but `fleet_update()` has a second failure mode that sets no `error`: the deploy completes and the post-deploy parity check rejects a host. The 2026-11-04 run on bender was exactly that — `fleet_sync` rc 0 and `status` rc 0 on beast, bender and mac, and `revision_parity: {ok: false, invalid_hosts: ["mac"]}` — so the state file stored `reason: null` and the one line the operator reads carried no information. `failure_reason()` now falls back to the parity step and names the host (`revision parity failed: invalid_host mac`), and says so plainly if a future failure carries no detail either.

### CI: the fabric agent watchdog tests always stop their watchdog thread
- Run 550 of an unrelated PR lost its whole unit-gates step at 87%: a `fabric_agent._watchdog` thread dumped every thread's stack and called the real `os._exit(1)`, which ended the pytest process (the log shows `FF`, no summary). Both watchdog tests stub `os._exit` with `monkeypatch`, which restores the real one when the test ends; `test_watchdog_exits_on_wedged_scan` also asserted `exited == [1]` although the stub returns, so the watchdog calls it again on every poll, and a failed assertion skipped `stop.set()`, leaving the thread to outlive the stub. Both tests now stop the thread in a `finally`, and the first asserts the exit code, not the count. This was not reproduced (6 of 6 runs of the old test passed with every core busy), so it is hardening that removes the way a watchdog can outlive its stub, not a proven fix of that one run.

### CI: every private test Postgres keeps dynamic shared memory out of /dev/shm
- Run 554 of an unrelated PR had 14 errors and 1 failure in `test_artifact_collab_api.py`: `asyncpg.exceptions.UndefinedFileError: could not open shared memory segment "/PostgreSQL.N"`, which the fixture then reported as `artifact store adapter failed to start`. Hosts that clean or shrink `/dev/shm` under a running postmaster break any parallel query that uses it. #518 fixed this for the e2e cluster (`tests/e2e/pg.py`) only; eight other helpers start their own socket-only cluster without the option. All eight now pass `-c dynamic_shared_memory_type=mmap`, and `test_every_private_postgres_launcher_keeps_dynamic_shared_memory_out_of_dev_shm` fails (naming the file) if any launcher that sets `listen_addresses` lacks it. The 519 tests of the suites that use these helpers pass.

### CI: the graph ingest-linearity check no longer fails on one stalled batch
- `test_review_fan_in_hub_reads_stay_fast_and_ingest_stays_linear` compared one second batch of 2,500 edges against `max(first * 3, 0.5 s)` and failed on loaded CI hosts (0.507 s and 0.533 s in two runs of one PR, 0.9 s for the whole test on an idle host, identical on main and on that PR's branch). It now times three later batches and asserts the best of them, the same way its read checks already take the best of three tries: a stall slows one batch, quadratic growth slows all of them. The bound is unchanged. Not mutation-tested against a deliberately quadratic ingest.

### Fix: the macOS fleet updater reads an existing agent's Label without `plutil`
- `scripts/fleet-auto-update-macos.sh install` decides whether an existing launchd plist is its own by reading `Label` with `plutil`, which exists only on macOS. On the Linux CI hosts the read failed, so `test_install_without_load_writes_the_agent_and_keeps_the_previous_copy` failed on a clean main and turned unit-gates red for every PR. The script now falls back to Python's `plistlib` when `plutil` is unavailable. A foreign label is still refused. All 12 tests in the file pass.

### Hindsight shared memory as an optional fabric service (unified memory P06; off by default)
- New compose profile `memory` in `deploy/control-plane/compose.yaml`: `hindsight-db` (pgvector pinned by digest, internal network only, named volume) and `hindsight-api` (loopback-only publish, pinned model directory mounted read-only, same hardening and limits as the herdr-memory deployment candidate). Nothing starts without `deploy.sh --with-memory`.
- New `control_plane.memory` block in the central fleet file (enabled flag, image tag, model dir, port, https endpoint, database password and four separately generated tenant keys: project, global, infra, personal). `herdr-eng fleet config memory init|status|endpoint|export-key` generates secrets on the host (0600, backup first, never printed); `render-compose` renders them only when enabled and refuses an incomplete block by key name. The non-secret endpoint is served to every host as `memory.url`.
- `deploy.sh --with-memory [--memory-checkout PATH]`: after the control plane is deployed and verified, a separate health-gated step builds the pinned herdr-memory image (`lock/upstreams.yaml` `herdr-memory-deploy`, amd64 only, refuses arm64 before anything changes) and recreates only the two memory services. A failed memory step exits 4, rolls the API back to the previous image (first deploy: stops the new containers), and never touches the control plane.
- Backups: `memory-backup.sh` + `herdr-hindsight-backup.{service,timer}` (daily, verified, owner-only dumps; `restore-test` into a temporary database). `herdr-eng doctor` gains container health, loopback `/health`, backup age, timer and endpoint checks; unreachable is `unknown`, never ok. `scripts/fabric-stack.sh memory-route enable` routes `https://memory.<domain>` and `https://memory.dev.lan` through the existing gateway certificates. See `docs/operations/memory-hindsight.md`.

### Team rooms, guest levels and the Suggest queue (unified workspace, slice 1; off by default)
- New `control_plane.collab.team_rooms` block (YAML only, hot, `enabled` absent = off, `kill_switch`, caps and a suggestion lifetime). With it on, a room owner can attach an explicit team to a room (`POST /api/v1/collab/rooms/<room>/teams/<team>/attach` with the owner's own agents per role: issue_writer, implementer, tester, reviewer) and detach it again. Attach and detach are separate collab events, the team's agents become ordinary room grants carrying the team id, and nobody in the room becomes a team member. Detach stays reachable with the flag off and removes only what the attach made.
- Each room has a guest level: `read_only`, `suggest` (the default) or `direct`, set by the owner only. `direct` is stored and validated but not implemented: it behaves as `suggest`, so no guest can assign work to an agent. A guest files a request that waits in the owner's queue; the owner approves (naming the agent, with the digest of the text they saw) or rejects. Approval creates one new task made by the owner with the guest's words attached as quoted data tainted `external-source:guest`, never as the command; repeating an approval, even after a crash, never creates a second task. At `read_only` a guest gets 403. While the flag is on guests no longer see the task list or task events, only their own requests, the room and shared artifacts.
- New `herdr-eng rooms attach|detach|teams|guest-level|queue|approve|reject|suggest|withdraw`, a room attention category `suggestion_decide`, and Work Graph `team_role` facts plus a team to room edge. No migration. Not built: web UI, the fleet-wide inbox, role routing, coordinators, assistant briefs, external actions. See `docs/operations/team-rooms.md`; threat model section 3.8.

### Team roles: routing by role, people as role holders, leases (unified workspace, slice 2; off by default)
- New `control_plane.collab.team_roles` block (YAML only, hot, `enabled` absent = off, needs `team_rooms` on too, `kill_switch`, lease, reminder, hard-expiry, extension and reviewer-group settings). The room owner can put people (members, including external guests) into a team's roles per room (`POST /api/v1/collab/rooms/<room>/teams/<team>/roles`); agents hold roles through the slice-1 attach. `POST .../tasks` then takes `target_role` (or `target_user_id`): the holder with the fewest open tasks wins, a tie goes to the smallest principal key, the resolved holder is written into the event so a replay never re-routes, and with nobody eligible the answer is 409 `no_role_holder` and nothing is written.
- A task for a person has no agent target, is in no agent inbox and is read only by the assignee, the room owner and its creator (`GET .../my-tasks`, answered with `POST .../tasks/<id>/respond`; attention categories `task_assigned` and `task_overdue`). A server-side sweep (no polling by the person, state rebuilt from the log, idempotent, no migration) sends reminders at 50% and 90% of the lease and then applies the team's no-response policy: `reassign` once then escalate, `escalate` (default; the task becomes `blocked` in the owner's attention) or `wait` (bounded by a hard expiry and an extension cap). With the flag on, independent review compares agent and person keys and honours configured reviewer groups. Owner-only `member_visibility` per room. Task fields the flag does not know are still a 400 with it off. Roll back the flag, not the image, while a task for a person is in the log (an older image breaks `room_state` and `room_flow` for that room).
- New `herdr-eng rooms roles|assign-role|clear-role|role-policy|assign|member-visibility|my-tasks|respond`. Not built: web UI, one-step re-issue of an escalated task, re-routing on a decline, email or push reminders, routing to read/post-only agents, coordinators, assistant briefs, external actions. See `docs/operations/team-rooms.md`; threat model section 3.8.

### Coordinators with limits (unified workspace, slice 3; off by default)
- New `control_plane.collab.coordinators` block (YAML only, hot, `enabled` absent = off, needs `team_rooms` and `team_roles`, `kill_switch`, `tasks_per_hour` default 20, `max_open`) and a second flag `coordinators.external.enabled` (off; `tasks_per_hour` default 5 and always lower, `max_open`, `max_depth`, `max_items_per_request`). The room owner can designate one of their own agents as a team's coordinator (`POST /api/v1/collab/rooms/<room>/teams/<team>/coordinator`), or, after a per-team opt-in (`.../external-coordination`, owner only), a guest of the room as an external coordinator with a new per-team `coordinate` right. A coordinator creates tasks only by `target_role`, only for its team, only under an operator-made accepted root task, within the caps; it cannot approve an external action, grant anything, change the guest level, the designation, the opt-in or the attached teams (403 and an audit event under its own identity). An external coordinator sees status only, no results, task list or assistants.
- A new guard stops an assistant from triggering an assistant, including through a third agent (400 `coordination_loop`), and independent review now excludes whoever decomposed the work. Revoking a coordinator (clear, opt-in off, removal, detach, revoked agent, or the identity provider dropping the person's group) cancels an operator-owned coordinator's open tasks and requeues an external one's to the role queue with a higher `attempt`, so the old run's update is a 409. No migration. Roll back the flags, not the image (the old image loses the requeue fence).
- New `herdr-eng rooms coordinator|coordinator-clear|external-coordination|coordination`. Not built: web UI, assistant briefs (slice 4), external actions (slice 5), Direct behaviour. See `docs/operations/team-rooms.md`; threat model section 3.8.

### Team-room screens in the collaboration app (unified workspace, web UI for slices 1-3; nothing new on the server)
- New `packages/collab-team-rooms.js` and three collapsed sections on the existing room page, none of which asks the server anything until it is opened (so a server with the flags off sees no new traffic and the room tab strip and its visual baselines are unchanged). The room owner gets one in the People tab: the Suggest queue first (the guest's words are shown in a labelled quote block as outside data, approve names the agent explicitly and sends the digest of the text that was shown, reject takes an optional reason, both ask first, and an approved request whose task was not created can be finished), the guest level (`direct` is labelled "acts as Suggest for now" and needs the agents or roles it may address), attached teams with attach and detach, role holders (people and agents) with the no-response policy and the member-visibility setting, and the coordinator with the per-team outside-coordination opt-in (off by default, its limits listed before the switch, confirmations on turning it on and off). Everybody gets "Tasks assigned to you" in the Tasks tab (accept, decline, done with a note, and a clear Overdue / Escalated state decided by the deadline); a guest also gets "Your requests to the room owner" (file, withdraw) and never sees an owner control, reason, command, target or task.
- Each flag is handled on its own: a 404 on a route the person may use is shown as a plain "not turned on" sentence (roles and coordinators say so separately from teams), never as an error. Guest and task text is escaped, never rendered as Markdown or a link, and never copied into a confirm dialog.
- The public listener and the collab edge only serve modules on an allowlist, so `/collab-team-rooms.js` is added to `_PUBLIC_STATIC` (`api.py`) and to the edge `location` pattern in `deploy/control-plane/collab-edge/nginx.conf`: without the edge line the public page would fail to load the module, so the edge config must be redeployed with this change (`test_public_static_imports.py` pins both). No server behaviour changed.
- 21 `CollabApi` methods for the team-room routes. 30 node tests (`packages/collab-team-rooms.test.js`) drive the real client against a real local HTTP server speaking the routes' contract. Not run: any browser. Not built: approving a request by role (the server only takes an agent), a team picker (the collaboration token has no team list, so the team id is typed), a fleet-wide inbox, assistant briefs, external actions, Direct behaviour. See `docs/operations/team-rooms.md`.

### CI: `test_real_monitor_surfaces_scope_and_invalid_config_in_the_list_bodies` no longer races the monitor's background loop
- The first GET of `/api/v1/github` starts the GitHub monitor's background loop, whose own pass reports `syncing` for a moment, so the test failed about 1 run in 5 on a clean main (seen as the only failure of an unrelated PR's unit-gates run). The test now waits, up to 10 seconds, for the real asynchronous state to reach `ready` before asserting. Nothing was skipped and no assertion was loosened. 15 of 15 repeated runs pass.

### Unified workspace: team collaboration design (docs only)
- New `docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md` describes how teams, rooms and personal assistants work together for one developer: a team attached to a room, three guest levels per room (read only, suggest by default, direct within limits), coordinators that are always the operator's own agents, roles instead of individuals (people included, with an attention queue and a no-response policy), and an assistant-to-team brief that carries a derived summary plus a reference only the operator can follow. It marks what exists on main and what is new, lists eleven points where the current code differs from the approved design (for example a team's members being its room's members, tasks that can only target agents, and the assistant acting as the room owner), sketches the data model, API and events, gives a threat analysis, a per-slice acceptance test list and the flags (all YAML, off by default), and records the operator's three decisions: issues are GitHub issues in a repository chosen per team or project, created only through an approval-gated external action keyed on the message id; every email-derived brief waits for the operator; and an external person may coordinate a team only through a per-team opt-in the operator sets (off by default), as their own identity, with a lower task cap, no approval or grant powers, no access to assistants or brief references, and revocation that requeues their open assignments. No code, schema or config changed; nothing is built. A pointer line is in `evidence/STATUS.md`; no phase state changed.

### Teams you create on purpose, and a "Your teams" page (unified workspace P06)
- New `herdr-eng teams create NAME [--project P] [--room R] | list [--status active|archived|all] | rename ID NAME | archive ID`, over `GET/POST /api/v1/graph/work/teams` (and `.../teams/<id>/rename`, `.../teams/<id>/archive`). Until now the Work Graph had a `team` kind but only each project's own team was ever written. A team made this way has a name, an optional project and an optional room; the people in the room are its members, so they (and operators) see it and nobody else is told it exists. A team with no room is visible to operators only. Only an operator key can create, rename or archive; archiving keeps the team and its history; repeating a create with the same name and links changes nothing (use `--idempotency-key` for a second team of that name). The routes stay 404 unless `control_plane.unified_workspace.enabled`, and 503 without Postgres.
- Teams are written as `fabric_events` rows (entity type `team`), like everything else the graph projects, so a rebuild from clean brings them back; no migration.
- New page `#/teams` ("Your teams" under Agents, listed only when the control plane advertises `unified_workspace_v1`): one link per team to its Team Map, archived teams behind a checkbox, plain states for off, unavailable, error and empty. Not yet proved in a browser (CI only). The home page is not changed.

### Artifact-room links: unlink, and link checks the room
- New `herdr-eng artifacts unlink ARTIFACT --room ROOM` (API: `POST /api/v1/artifacts/<id>/links/remove` with `{"room_id": ...}`) removes every link between an artifact and a room. It needs `edit` on the artifact, like `link`, answers a caller without it exactly like a missing artifact (404), and is safe to repeat (`unlinked: 0`). With room-derived access on, it also removes that room's read grants on the artifact at once; people another room still links it to keep access through that room. Before, a link could not be removed at all.
- `artifacts link --room` now answers 404 `not found` for a room that does not exist (checked whenever the control plane runs the collaboration service); an API key has no room membership, so existence is all that can be checked for it. Threat model R-ART3 and R-RA3 are mitigated, not closed: an editor can still link to any existing room. See `docs/artifacts/room-access-maintenance.md`.

### Room-derived artifact access: never overwrite another source, and a way to clean up
- The room sync no longer replaces a grant that another source holds. A person with a direct grant on an artifact (an explicit write or share grant, any `granted_by` that is not `room:*`) keeps exactly that grant when a room links the artifact, and it is not revoked when they leave the room; before, the room wrote its `read` over it and a later revoke deleted it. Between two rooms the first holder keeps the row unless the second room's grant lasts longer, so a guest-style expiry never shortens a member's grant. One grant row still holds one source (a direct grant that later lapses is not replaced until that room's next sync); no migration.
- New `herdr-eng control-plane revoke-room-grants [--fleet-config FILE] [--apply]` deletes every `artifact_acl` row with `granted_by` `room:*`, for use after `room_artifact_access` was turned off (turning it off never removed the grants already written). Dry run by default, never touches any other source, safe to repeat; restart the control plane afterwards. See `docs/artifacts/room-access-maintenance.md`. Threat model R-RA2 is resolved apart from the one-row-one-source limit above.

### Crash takeover: the lease fence is no longer optional after a requeue
- Once a lease has requeued a task (`attempt` above 1), `PATCH` task updates that omit `attempt` are refused with 409 `stale attempt`, the same as an old `attempt`; before, only a request that sent a wrong `attempt` was fenced, so an old run that never sent one could still accept or finish the requeued task. Tasks never requeued, and every deployment with `control_plane.collab.task_lease_seconds` unset, behave exactly as before. The MCP `task_update` tool takes an optional integer `attempt`, and `worker.sh` (and the copies the dashboard and host runner hand out) now sends the `attempt` that `tasks_inbox` gave it, so those runners keep working after a requeue. A custom runner has to echo `attempt` from `tasks_inbox`. Threat model R-TL1 is resolved; R-TL2 (no heartbeat) stays open.

### Unified workspace P09 (docs only): threat model and rollout plan
- `docs/specs/herdr-unified-workspace-20261028/security/THREAT_MODEL.md` now has an implementation threat model of what is on main: work graph routes, artifact registry and ACL, room-derived read access, share links (lab and public), crash-takeover leases, the collab public listener and the control-plane deploy, each with mitigations, residual risks and a severity, plus a turn-on, watch and turn-off table per flag and an accessibility checklist for screens not yet built. It was written by reading the code; nothing was run or verified. No code, schema or config changed. P09 is marked partial in `evidence/STATUS.md`; `evidence/P09-RESULT.md` lists what is not done and the code risks found.

### CI page: the history report is built once per few seconds
- `GET /api/v1/ci/reports` took about 5 s per call and 6, 12 and 17 s for three at once, because every open CI page rebuilt it on every refresh. Pages that gave up showed "CI history: Failed to fetch" and left broken-pipe tracebacks in the log (106 in 40 minutes). The first caller now builds it and concurrent callers wait for that one result; a result is reused for up to 15 s (the page refreshes every 20 s) and only while no CI observation, report or log-test row has been added or replaced, so an upload shows at once. A failed build is not cached. What the endpoint reads from the CI monitor and GitHub language cache can be up to 15 s old.

### Artifact share links: share with a room
- `herdr-eng artifacts share create ... --audience lab --room ROOM` (API field `room_id`) adds the sign-in emails of the people in that room, as they are when you create the share, to the lab allow-list. The link is still the lab link: Cognito sign-in, exactly those people, a frozen bundle on the share volume, no path to the registry or the tailnet. People who join the room later are not added (create a new share); lapsed guests and people with no known email are left out; a room with none is refused. Public shares refuse `room_id`.

### Team Map page for one project (unified workspace P06)
- New page `#/team-map?root=project:<id>` (sidebar "Team map" under Agents, listed only when the control plane advertises `unified_workspace_v1`, i.e. `control_plane.unified_workspace.enabled: true`). It draws the project's Work Graph as columns with labelled lines (works together / handed work to / waits for / produced / reviewed; earlier relations faded and marked), counts milestones ("1 of 2 tasks done", never a percentage), filters by text, kind and status, folds agent sessions and hosts away until asked, opens a details panel, and always offers the same map as a written list. Nodes work from the keyboard (arrows, Enter, Alt+arrow to move). "Save my layout" keeps your own positions; nothing already placed moves when new work appears, and a layout changed elsewhere is not overwritten. With the flag off the page says the Team Map is not turned on. Not yet built: the "Your teams" home and the Conversation view; there is no team producer, so you open a project by its id. Not checked in a browser here; CI's browser gates are the first run.

### Team Map logic (unified workspace P06, not yet on a page)
- `packages/team-map-model.js` groups a Work Graph snapshot project -> issue -> task -> deliverables, folds noisy execution nodes into a count, filters, names what each line means (five meanings), describes the map as text, counts milestones instead of inventing percentages, and lays nodes out deterministically without moving placed ones. `HerdrClient` gains `workGraphSnapshot`, `workGraphLayout`, `saveWorkGraphLayout`. Nothing renders it yet.

### CI page: one attention item per repository
- "Needs attention" on the CI page no longer lists the same repository once per pull request and branch. Unfixed failures of one repository are one row: the newest failure leads (its failing step and log load when the row is opened), and the other failing branches and pull requests are listed inside it, with a "N branches or PRs failing" chip. Each ref is still judged on its own, so a green main does not clear a red pull request, and a ref that gets a green run drops out of the row. The Overview badge counts repositories.

### CI: throwaway test Postgres no longer uses /dev/shm
- The Postgres cluster the tests start now uses `dynamic_shared_memory_type=mmap`. CI runs 414 and 415 failed `unit-gates` with 8 `test_postgres_recovery.py` errors, "could not open shared memory segment /PostgreSQL.N": parallel-query memory lived in `/dev/shm`, which a CI container can shrink or clean under a running cluster. This is the likely cause, not a proven one: watch `unit-gates` over the next runs.

### Artifact registry: room-derived read access (unified workspace P03)
- With `control_plane.unified_workspace.enabled: true` AND `control_plane.unified_workspace.room_artifact_access: true` (YAML only, both default off), people in a room can read the artifacts linked to that room. It is written as ordinary registry grants, not a second access check: `read` for `collab:user:<id>`, `granted_by: room:<room id>`, expiring with a guest's membership. Grants are re-synced when membership changes, when an artifact is linked to the room and at startup; a person who leaves loses access. New route `GET /api/v1/collab/rooms/<room>/artifacts` (members only; agents and non-members get 404) lists them through the registry's own grants. Room membership grants `read` only, never edit, share, review or admin. Known limit: one grant row holds one source, so a person and artifact shared by two rooms flip between them on each sync (access stays while either room has the person; only guest expiry times can differ).

### Artifact gallery: filter by linked work (unified workspace P03)
- The Artifacts page can list what is linked to a project, issue, task or room ("Linked to" + id, through `/api/v1/artifacts/for/<kind>/<ref>`, which already filters by what the signed-in user may read), and an artifact's page shows a "Linked to" section. Type and status filters apply to the full list only. The web client gains `artifactsForContext` and `artifactLinks`. No server change.

### Artifact registry: link from the CLI (unified workspace P03)
- `herdr-eng artifacts links <id>` lists an artifact's bindings and `artifacts link <id> --project|--issue|--task|--room [--version V] [--link-kind origin|reference|derived]` adds one through the existing links route (idempotent; refused client-side with no target). Searching a project, issue, task or room is `artifacts for <kind> <ref> --query`. There is still no `diff`: the server has no diff route.

### Collaboration tasks: crash takeover (unified workspace P05)
- `control_plane.collab.task_lease_seconds` (60 to 604800, YAML only, read at startup, absent = off) turns the lease on. A change needs a restart; it is not a hot switch.
- `CollabService.task_lease_seconds` (default None = off): an accepted task whose lease ran out goes back to pending for its target agent when that agent polls its inbox, as a replayable `task.requeue` event. Each task carries an `attempt` (starts at 1); an update that names a stale `attempt` is refused with 409. No fleet-config key yet, so nothing changes in production.

### Artifact registry: read-only CLI (unified workspace P03)
- `herdr-eng artifacts show <id> [version]`, `versions <id>` and `for <kind> <ref> [--query --media-type --limit --cursor]` browse the versioned registry through the existing read routes. Nothing new is exposed server-side; an unreadable artifact answers "not found, or you cannot read it". The key is read from the environment variable named by `--api-key-env` and never printed.

### Artifact registry: scheduled reconcile (unified workspace P03)
- The worker can now run the registry's reconcile pass on a schedule: `control_plane.artifact_reconcile: {enabled, interval_seconds, grace_seconds, orphan_ttl_hours, limit}` in the fleet file (YAML only, off unless `enabled` is exactly true). It marks orphan blobs (never deletes them), closes expired uploads and purges tombstones past their hold window; lineage re-emission is not part of the scheduled pass. A failing pass is logged and never stops the worker.

### Unified workspace P05: safe retry of a task completion
- `POST /api/v1/collab/tasks/{id}/update`: repeating a `done`/`failed` update with the identical result now returns `{"status": ..., "replayed": true}` and writes no second event (the agent's answer was lost, or it restarted). A different result or outcome on a finished task is still a 400, and a repeated `accepted` is still a 400 as before.

### Unified workspace P05: review lineage in the work graph
- A task created with `reviews_task_id` now folds into the work graph as a `depends_on` edge from the review task to the work task (the contract's existing edge, dependent -> prerequisite) and a `reviews_task_id` attr. No new edge type, no schema change.

### Unified workspace P05: independent review enforced where tasks are created
- `POST /api/v1/collab/rooms/{id}/tasks` accepts an optional `reviews_task_id`. A review task is refused (400) when its reviewer is the agent that did the work or the agent that handed it out, and an agent cannot choose the reviewer of its own work; an unknown task or one from another room is a 404. The id is stored on the task and in the `task_created` event. Without the field nothing changes, and idempotency keys keep their meaning (the request digest only gains the id when it is present).

### Unified workspace P05: durable task leases (migration 0040)
- `team_task_leases` (additive, idempotent, `-- DOWN:`) and `team_task_leases_pg.PgLeaseStore`: the same fencing-token lease contract as the in-memory `LeaseStore`, every transition one conditional statement so two control-plane processes cannot both hold a task. Tests run the lease contract against Postgres plus a 12-way concurrent acquire (exactly one winner). Still not called by any route.

### Unified workspace P05 (partial, library only): team templates, task admission, reviewer separation, leases
- New `herdr_engineering/control_plane/team_tasks.py`: `TeamTemplate` roles (planner, implementer(s), one independent read-only reviewer, optional coordinator) that cannot name a host; `admit` lets a task in only from an authorized human through the P01 broker (`submit_task`), never from a message or tainted content, with role-bounded actions, template budget, deadline, acyclic dependencies and reviewer independence; `LeaseStore` gives one lease per task with monotonic fencing tokens (exactly-once completion, crash takeover fences off the stale worker, abort or expiry authorizes nothing).
- Nothing calls it yet: no routes, no persistence, no Pi mission admission, no `/assign`/`/tasks`/`/skill` wiring, no scheduler placement. Tests: `tests/control_plane/test_team_tasks.py` (18, each safeguard mutation-checked).

### Artifact registry: provenance, context lookup, resumable uploads, lifecycle (unified workspace P03 foundation)
- Migration `0031_artifact_registry_extensions` (additive, idempotent, `-- DOWN:` in the file). Versions gain typed provenance (origin room/task/issue/project, session, agent, model, prompt/skill revision, commit, redacted inputs) with **no foreign keys**, and a trigger makes committed content and provenance immutable and undeletable. Bindings gain task/project/link kind so one artifact can belong to several tasks.
- `GET /api/v1/artifacts/for/{project|task|issue|room}/{ref}`: artifacts for a work object, filtered by the registry ACL in SQL before counts, pagination and search; a missing context and a context with nothing visible are the same empty page. `GET|POST /artifacts/{id}/links` for post-hoc, idempotent links.
- Resumable uploads proxied through the control plane (`POST /artifacts/{id}/uploads`, `.../parts/{n}`, `.../complete`, `.../abort`): whole-file SHA-256 and size verified, parts staged under `staging/` only. No browser-to-S3 traffic and no CSP change.
- Lifecycle: `archive`, `restore`, `tombstone` (reason required, 30 day hold or legal hold) and a purge that deletes bytes only when no other live artifact shares the content-addressed key; rows, hashes and the append-only event trail remain.
- `POST /artifacts/reconcile` (operator): re-emits lineage for versions committed but never linked, marks (never deletes) orphaned blobs, expires stale upload sessions, runs due purges. Rollback: deploy the previous control-plane image first (commit_version writes the new columns), then apply the DOWN block; the migration is additive, so leaving it applied under old code is harmless.
- Design: `docs/specs/herdr-unified-workspace-20261028/design/ARTIFACT_INTEGRATION.md`.

### Rakazo bridge: slow calls are logged
- A bridge call that takes 300 ms or more logs `rakazo bridge: <procedure> took N ms (status S)` (a warning from 2 s). Only the procedure name and the duration are logged, never bodies, tokens or query strings. A stream's duration is its setup only.

### Assistant: scheduled CI watch
- `control_plane.personal_assistant.ci_watch` (YAML, off by default: `enabled`, `interval_seconds` min 60 default 300, `users: [{tenant_id, user_id, assistant_id?}]`, `auto_grant` default false, `source`, `max_age_seconds`, `tick_budget_seconds`) runs `watch_ci` periodically from a deterministic server thread (actor `system:ci_watch`, jittered start, time-budgeted, never raises, stops with the server). Per user it skips quietly when the assistant, project intelligence or scope is missing or the kill switch is on; a missing `watch_ci` grant logs one WARNING per user per hour unless `auto_grant: true`, which creates an audited standing grant.
- `watch_ci` now also reads failed pipelines from the CI monitor's cached snapshot: the lineage `ci` nodes the deployed flow writes carry no repository or run number, so they could not alert. Only the scheduled runner uses it (the manual/tool path stays lineage-only); scope-filtered before a row cap, finite age bound (unknown time dropped), `killed` is not a failure, one `(repo, run, head)` fingerprint for both sources, at most `max_new_per_tick` (10) new proposals per user per tick, and `auto_grant` never re-creates a revoked grant.
- `GET /api/v1/intelligence/status` (operator) reports `ci_watch` (last run, counts, fixed skip reasons). The server logs the Rakazo bridge assistant user ids at startup. See deploy/control-plane/README.md.

### Rakazo mobile bridge (off by default)
- `control_plane.rakazo_bridge` (YAML: `enabled: false`, `accounts: [{email, password_scrypt, role: operator}]`, `token_ttl_days: 30`, `max_tokens: 5`) lets the Rakazo iPhone app use the Herdr Personal Assistant as a custom server. `/rpc/*` and `/api/auth/*` serve the subset of Rakazo's protocol the app needs (sign-in, `me`, bots, threads with a live event stream, approvals via `threads/answer`, runs); everything else answers a Rakazo-shaped 404. Disabled, those paths stay 404. An enabled but malformed section stops startup.
- Auth: email + scrypt-hash password (`herdr-eng bridge hash-password` reads the password from a prompt or stdin, never argv), constant-time compare with one KDF per attempt even for an unknown email, per-address and global exponential lockout, 32-byte bearer tokens of which only the SHA-256 is stored (migration `0036_rakazo_bridge_tokens.sql`, persisted when a Postgres DSN is configured), a per-account token cap, sign-out revokes. No password, token, hash or request body is logged or audited.
- Each account is its own assistant user, so a second account (shape reserved for later multi-user; only `role: operator` exists, `family` is rejected) gets separate threads, memory, approvals, audit identity and rate limit. Approvals decided in the app use the existing `decide_approval` rules (approver must differ from requester unless YAML allows self-approval).
- Hardening: revocation survives a failed database delete (in-memory tombstone, retried with backoff; sign-out reports it) and `accounts[].tokens_valid_after` cuts off older tokens independent of Postgres; no all-addresses lockout (per-address only, so a tailnet device cannot lock the owner out); database I/O runs outside the token lock with 3 s timeouts; `trusted_proxies` lets the lockout key on the real client behind the fabric gateway (which is not loopback); hostile bodies get a generic 400; `runs/list` is bounded and lock-safe; 20 bots per account.
- Residual risk: a token is operator-level assistant access from anywhere that can reach the listener (the tailnet). No push, voice, billing or model screens. See deploy/control-plane/README.md.
- Board proposals on the phone: pending project-intelligence proposals (`ci_rerun`, `request_ci_repair`, `propose_issue`, ...) in the account's scope (`assistant_viewers`) appear as approve/reject `ask` cards in its first bot's thread, with an in-thread notice, expiry, and an outcome message (executed / failed with the recorded reason / rejected / expired). `threads/answer` calls the new shared `IntelligenceRoutes.decide_proposal` (extracted from the dashboard route: flags, approvers, project scope, exact payload hash + version, audit, executor), so execution still needs `project_intelligence.execute`. Fail closed without a scope or approver standing; card text uses validated fields only. No push (custom server); see deploy/control-plane/README.md.
- Phone approvals answer immediately: `threads/answer` for a proposal records the decision and returns, and the executor (e.g. the Woodpecker restart behind `ci_rerun`, several seconds) runs on a bounded background worker (4 at most, no queue; a fifth is refused with 429 undecided) that then updates the card and posts the outcome. `IntelligenceRoutes.decide_proposal` gained `execute=True` (the dashboard is unchanged).

### Assistant: CI failures also queue a re-run proposal
- When `watch_ci` sees a new CI failure it now queues, beside the repair proposal, a separate `ci_rerun` proposal on the same board (same scope rules, same one-time dedupe). The repair proposal records it as `rerun_proposal` and the notification says a plain re-run awaits approval. A refused or failing re-run request never hides the repair proposal. Approving it still needs `project_intelligence.execute: true`.

### Project intelligence: `ci_rerun` action (ask mode)
- A new approvable proposal action `ci_rerun` (`{repo: owner/name, run: N}`) restarts one Woodpecker pipeline run. It goes through the existing board: `suggest` to propose, an approver (or the single operator) to approve the exact payload, and `execute: true` before anything runs. Execution calls the CI monitor's own retry, so `control_plane.ci_actions` and Woodpecker's `can_retry` still decide; a refusal is recorded on the proposal and never retried. `execute` stays off by default.

### Unified workspace P02: persistent Work Graph and reconciliation (flag off by default)
- Migrations `0033_event_trace_id.sql` (nullable `fabric_events.trace_id`, `Event.trace_id`, envelope `trace_id`) and `0034_work_graph.sql` (`wg_*` tables: event ledger, assertions, entities + aliases, typed edges with intervals and unique `(type, from, to)`, historical facts, layouts, cursor, gaps). Additive, idempotent, `-- DOWN:`.
- `work_graph_core` (pure fold: idempotent, order-tolerant, per-field merge, tombstones that cannot be resurrected, immutable artifact versions), `work_graph_store` (Postgres, ACL inside every statement, cursor paging, search, snapshot by root, `as_of`, facts, layouts with ETag/If-Match), `work_graph_sync` (sources, reconciler with orphan check in both directions, replay-from-clean with `snapshot_required`, worker), `work_graph_service` (sync facade + request handler).
- New `GET/POST /api/v1/graph/work/*` and `/api/v1/collab/graph/work/*` behind `control_plane.unified_workspace.enabled`; 404 when off, 503 without Postgres. The lineage graph vocabulary gains the owned kinds `project team artifact_version external_action` and edges `contains derived_from discussed_in approved_by notified_via`.
- Contract: `docs/contracts/work-graph-v1.md`; Pi session-ingest ids: `docs/specs/herdr-unified-workspace-20261028/contracts/PI_INGEST_FIELDS.md`.

### CI test counts from logs, with no project setup
- The control plane reads the step logs of recently finished Woodpecker runs and keeps what the test runners printed (pytest, `node --test`/TAP, cargo, `go test -v`, jest/vitest, ScalaTest, surefire, gradle, Karma, `unittest`): counts, runner names and, where the runner prints one line per test, an index of names, statuses and log line ranges. No log text is stored, a JUnit upload for the same run always wins, and no environment variables or pipeline changes are needed. `control_plane.ci_log_tests` (YAML: `enabled`, `runs_per_cycle`, `max_log_mib`, `window_days`) tunes it.
- `GET /api/v1/ci/log-slice` returns the redacted live log lines around an indexed test, or says why it cannot.
- The CI Tests tab bars use these counts and say where they come from; selecting a repository lists its tests (search, filter, sort) and opens a log excerpt with ways to keep investigating.

### Unified workspace P01: contracts, ids and the capability/action broker (library only, flag off)
- New `schemas/workspace-v1.schema.json` (JSON Schema 2020-12) for project, team, room, mission, issue, task, agent, agent_session, host, artifact, artifact_version, review, decision, external_action, event and the 14 typed edges, with canonical ids `urn:herdr:<kind>:<uuid>` (uuid5 over the native id, so every host agrees), aliases, scope, version/ETag, trace/correlation/causation ids and reference URIs. Contract table and graph.py mapping: `docs/contracts/unified-workspace-v1.md`.
- New `herdr_engineering/control_plane/workspace_contracts.py`: id/alias canonicalisation, validators, ETag compare-and-swap, idempotency tokens, a discover/authorize/approve/invoke broker (user role, agent delegation, room grant, artifact grant, account grant and risk policy are intersected; room membership never grants shell or external-account access), untrusted-content tainting, ACL re-check hooks for traversal, search, export and subscriptions, and the replay-cursor contract.
- Capability `unified_workspace_v1` in `/api/v1/health`, only when `control_plane.unified_workspace.enabled: true` (YAML, default off). No existing route, table or graph vocabulary changes; nothing calls the broker yet. `jsonschema` joins the `dev` extra for the contract tests.

### Skill registry on registry artifacts (flag off)
- `control_plane.skill_registry.enabled` (YAML, default false) adds `/api/v1/skills` publish, search, get, 60-second transfer capability, download and install receipt. Skills are `artifact_type='skill'` registry artifacts (immutable versions, signed package in RustFS, sharing = artifact ACL grant); no new tables, no CSP change. Extracted from #366. See `docs/contracts/skill-registry.md`.

### Control plane: inspector replay snapshots (opt-in)
- `control_plane.inspector_snapshots.enabled` lets the worker keep a replay snapshot of the inspector in the new `inspector_snapshots` table (migration 0030) so a boot replays only newer events instead of ~780k (about nine minutes). Off by default; any doubt makes the boot replay everything. See `deploy/control-plane/README.md`.
- With a verified snapshot the boot also skips loading the inspector rows it covers (SQL filter on the watermark; the repo fetches them back if it refuses the snapshot).
- Deploy health window 30 minutes (#373) and boot phase timings in the log; host-metrics rollups replay in linear time (#379).

### Control plane: service-observation compaction (opt-in, dry run by default)
- `control_plane.fabric_compaction` (YAML; `enabled: false`, `dry_run: true`, `interval_seconds`, `min_age_days`, `batch_rows`, `chunk_rows`, `chunk_pause_seconds`, `max_pass_seconds`, `archive_retention_days`, `rebuild_checkpoint`) lets the worker move interior service observation rows out of `fabric_events` into the new `fabric_events_archive` table (migration 0035), one atomic statement per chunk of `chunk_rows` (default 5,000; the pass lists candidates once, pauses `chunk_pause_seconds` between chunks and stops after `max_pass_seconds`, so a large backlog no longer stalls ingest). Rule checked on the production journal: 1,762,189 service rows -> 349,664 with all 6,544 reconstructed services identical. Rows with a v2 envelope and rows younger than `min_age_days` are never touched. After a drain the job rebuilds the recovery checkpoint; the archive is purged after `archive_retention_days`. See docs/operations/recovery-checkpoints.md.

### Deployment escalation issues can use the GitHub App (`escalations` capability)
- With `github.app.write: [escalations]` (App permission Issues: write) escalation issues are created with a repo-scoped App token. Without it the server-held `github.token` is used as before; an enabled capability or an invalid `github.app` block that cannot mint a token fails closed and never falls back to the PAT.

### GitHub App: write capabilities and the review gate on the shared App (opt-in)
- `github.app.write: [review_gate, escalations]` (YAML, default none) enables fixed, repo-scoped write permission sets on the shared GitHub App; unlisted capabilities are refused before any call to GitHub, reads stay read-only, and a grant wider than the request is refused.
- `review_gate.app: shared` makes the review gate use that App instead of its own JWT/installation-token code: reads get a read-only token, statuses/merge/update-branch the `review_gate` capability. The old per-gate `app:` block still works.

### Project intelligence: shared-credential PR poller and single-operator mode (opt-in)
- The outbound PR poller now works without the review gate: it reads open PRs through `github_auth.credentials()` (the GitHub App, `pull_requests: read` only, repo-scoped tokens). `github_auth` allows `pull_requests` at `read`; nothing writable. `/api/v1/intelligence/status` gains `poller` (counters and a fixed `last_error`).
- `control_plane.single_operator: true` (YAML, default false; refused with `api_keys`) lets the open listener's caller preauthorize assistant tools and approve proposals, including their own. See the project-intelligence runbook for the residual risk.

### Control plane: recovery checkpoint pruning
- After the compaction job rebuilds the recovery checkpoint it deletes all but the newest `control_plane.fabric_compaction.keep_checkpoints` generations (default 2; the active one is always kept) under the builder's lock, so generations no longer pile up. `recovery_checkpoint.prune_checkpoints()` is the same operation for manual use.

### Dashboard: on-demand full `/summary` report
- **Generate full report** on the agent page runs the `summary` skill on the session's recorded conversation and shows the result in the summary card; in the fullscreen workspace it gets its own column so the conversation and composer fit the viewport without page scrolling (the fullscreen panes were previously sized to the whole viewport and cut off the composer). Off by default: `summary_report.enabled` in the central YAML.
- Runs on the host that owns the session, not in the control-plane container (which has no session records and no `claude`): the control plane queues the request (`POST .../summary/report`, durable event), the per-host session service claims it (`POST /api/v1/host/summary-reports/claim`), runs `herdr-eng summary report` locally and posts the result back, which the server validates and redacts again before storing. Unclaimed requests fail as `host_unreachable`, stuck ones as `timeout`. Roll the plugin to each host.
- Read-only by construction: the host collects the session history and read-only git facts itself and gives a fresh, non-persisted `claude -p` run no tools; the target session is never resumed or prompted. The report is validated (`# Session summary` ... Evidence coverage) and served as `full_report` on `GET /api/v1/sessions/{id}/summary`.
- `herdr-eng summary report` prints the same result as JSON. `herdr-eng summary history` now returns the session's recorded `cwd` and finds a session by id without `--agent`. The skill gains a target mode (`/summary <agent> <session-id>`, optionally with prepared `SUMMARY_EVIDENCE_JSON`). Codex as summarizer is implemented but not enabled until it has run live. See `docs/operations/session-summary.md`.

### Dashboard and terminal: `/summary` publishes, the report is easier to read
- Terminal `/summary` and `$summary` publish the report to the dashboard (`herdr-eng summary publish`, `POST /api/v1/host/summary-reports/publish`) and open a read-only side pane, then print a single line instead of scrolling the whole report. If publishing fails the full report is printed.
- Dashboard: **Quick summary** / **Full report** buttons; a status strip with overall status, evidence-coverage badge and resume point; collapsible sections; a stale badge ("N newer messages since this report") with one-click regenerate; elapsed time while generating; **Copy as markdown** and **Send to agent**; timestamps that jump to the matching turn; a failed quick summary shrinks to one line when a full report exists. Fullscreen hides usage rows by default and the Activity pane can be collapsed.
- Evidence: the history helper records every distinct `cwd`, and the report inspects the latest worktree that is a repository and lists earlier ones as `other_worktrees` (it previously inspected the first directory, which was wrong once a session moved into a worktree). Jobs and completions carry `basis_seq` for the stale check.

### Fix: idle Codex agents showed an empty dashboard and could not be summarized
- Codex does not keep its rollout file open between writes, and Herdr reports no `agent_session` for it, so a Codex agent's dashboard page said "Waiting for this agent to identify its transcript" with no conversation, no activity and `recording_unavailable` in the summary card. The feed plugin now also reads Codex's own state database (`logs_*.sqlite`: owning `pid:<pid>:...` and `thread_id`) as process evidence. It is the weakest evidence (only used when no fd, descendant or native identity names a conversation), rows must be newer than the process (reused pids are rejected), only a primary conversation counts, and a tie is never guessed.
- `herdr-eng summary history` uses the same rule, so `/summary` and the dashboard agree on which Codex conversation a pane holds. Deploy the plugin (`deploy/herdr-cp-feed-plugin`) on each host to pick this up.

### Fix: macOS release staging
- `docs/specs/herdr-codeboarding-native-port-2026-10-23/AUDIT.md` collided with `audit.md` on case-insensitive filesystems, so every fresh checkout of `main` since #194 looked dirty on macOS and `fleet-sync` refused to stage it. The requirements stub is now `AUDIT-REQUIREMENTS.md`.

### `/summary` session-recovery skill (default install and fleet converge)
- New canonical read-only `summary` skill (`skills/summary`) installed by `scripts/install.sh` and converged on every fleet host (`ansible/tasks/summary-skill.yml`). `/summary` in Pi, Claude Code and OpenCode; `$summary` in Codex (no Codex command file is written). Evidence-based: separates verified from reported work and states an explicit coverage verdict.
- `herdr-eng summary install|uninstall|verify|history`. Install/upgrade is manifest-tracked and never overwrites a customized skill, prompt or command (`preserved`); uninstall removes only unmodified files and our own links. `history` is read-only: it resolves the session from an explicit id, then Herdr pane metadata, and only then a labelled directory heuristic; it reads Claude/Pi/Codex JSONL and OpenCode SQLite (`mode=ro`), marks compaction and child-agent gaps, and redacts output. See `docs/operations/session-summary.md`.
- `herdr-eng fleet status` now shows `summary=current|differs|MISSING|unknown` per host (informational; never fails parity, since a customized skill is deliberately preserved).

### Collab addressing (`@owner.harness`, sigil completion, mention chips)
- Release note: a room task's text now starts with the target's `@address` (e.g. `@gert.pi fix the build`). The worker in this repo strips its own leading address; worker scripts installed from an earlier release pass it through to the harness unchanged, so update them.
- An external (sponsored) agent never gets an `@owner.harness` address; it is reached by its `ext-...` name and its chip is marked external. A person handle outranks an agent name, an agent cannot be renamed to (or granted under) a member's handle, and an agent keeps its last 5 old names as aliases.

### T17 / ADR-0008 P2: peer registry, room shares and signed read-only export (default off, YAML only)
- New: `control_plane.federation` gains the peer registry (`public_key` pinned per peer, per-peer `enabled`, `export_rooms`, `requests_per_minute`, `max_bytes_per_hour`, `key_expires_at`, `rotate_after`, `next_public_key` for rotation overlap) plus `server_id`, `signing_key_file` and the validity knobs. Still default off, hot-reread, no environment variable; an unreadable file, an invalid section or a non-boolean `enabled` is off.
- New collab events `federation_share_granted` (revocable creation), `federation_share_revoked` (in `SAFETY_EVENTS`, pre-paid), and `federation_peer_enabled` / `federation_peer_disabled` (the YAML change record and its pre-paid revocation). A room owner accepts a share (`POST /api/v1/collab/rooms/<room>/federation/shares`); the operator YAML must also list `{peer, room}`; any owner revokes, and the kill switch never blocks a revocation. Members can see that their room is shared.
- New `GET /api/v1/federation/shares` and `/shares/<id>/events`: read-only, authenticated by an Ed25519 request signature (pinned FULL key, audience, timestamp window, single-use nonce), answered with exports signed over the exact bytes, a bounded validity window, a per-type FIELD ALLOWLIST (`message` only; a pseudonymous sender, no ids, emails, mentions or context) run through `redact.py`. `org`/`private`/`mission` scope and history from before the grant never leave; every unshared, revoked, unlisted or unauthenticated case is one byte-identical 404.
- Fixed (P1 gap): `PeerPolicy.matches` compared only 12 hex characters of the key fingerprint. It now compares all 64 (`hmac.compare_digest`); the peer principal carries the full fingerprint and a peer ingest event is admitted only for the pinned key. `mirror_room` is now optional (an export-only peer needs none; a peer without one can never write). A P1-shaped YAML (a 12-64 hex `key_fingerprint` and no `public_key`) still parses but can no longer authenticate or ingest: add `public_key`.
- No puller, importer, mirror room or typed write (P3/P4). Signing uses `cryptography` (already installed with the `collab` and `review-gate` extras; new `federation` extra names it), imported lazily: without it federation is off. Contract: `docs/contracts/federation-export.md`. Rollback: revert the PR (kill switch `control_plane.federation.enabled: false`).

### T17 / ADR-0008 P1: `peer` principal and visibility clamp (flagged dark, no network code)
- New per-principal visibility clamp in envelope normalisation (`federation.Clamp`, `normalize_envelope(..., clamp=)`): an event from a `peer` principal is rewritten to `visibility_scope: room` in its one local mirror room, never wider, with the sender's origin, requested scope and local ids kept only as `claimed_*` provenance. It cannot be skipped: a peer without a clamp, and any v1 or non-`lineage` peer event, is refused.
- New `peer` principal kind, never `operator`, `collab` or `external`, and never mintable from an API key. `control_plane.federation` (YAML only, default off, hot-reread, any error means off; `"*"` rejected for a peer) is parsed and enforced; startup refuses a bad section. `_check_lineage_scope` and `_check_reserved_nodes` treat a peer as non-operator.
- New reserved lineage namespace `peer:<peer>.` (node type `peer`): writable by nobody locally (operator included); a peer may write only inside its own namespace on every edge endpoint. Mirrored edges take no part in local gate staleness (fork promotion, herdr#264).
- No endpoint, puller, network code, environment variable or migration. Rollback: revert the PR (the kill switch is `control_plane.federation.enabled`, default false). Contract: `docs/contracts/event-envelope-v2.md`.

### T17 Independent-server federation ADR (docs only)
- ADR-0008 is now Accepted with the owner's answers to D1–D10 recorded; implementation starts with P1 (visibility clamp and `peer` principal kind), flagged off.
- `docs/adr/ADR-0008-independent-server-federation.md`, status Proposed, awaiting approval: options, trust model, data flow, failure modes, migration, non-goals and ten owner decisions. No code, configuration, endpoint or migration; nothing is implemented until the ADR is approved. Rollback: revert the PR.

### T16 Herdr-side gaps from the Pi and AutoSpec wiring
- Steering: an ingest key can read the CURRENT fence and gate digest at apply time: `GET /api/v1/steering/targets/{node_id}` (owner of the node, live
  request, room-scoped nodes included) and `fence`/`digest`/`digests` on `GET /api/v1/graph/nodes/{id}` for nodes the key may read. A `fork` may carry
  a bounded, redacted `instruction` (the changed requirement), delivered in the pending item like a redirect's.
- Review quorum: `POST /api/v1/graph/nodes/{pr}/review-quorum/rereview` records a durable, idempotent-per-head, scoped (trusted head writer, reviewer or
  operator), rate-limited, audited re-review request; the quorum read shows it as `rereview_request` for that head.
  A head may be asked about again once its latest ask is older than `review_quorum.rereview_retry_seconds` (YAML, default 3600); inside that window
  AutoSpec owns retries. Review-gate worker keys are refused on the route (403); the gate's own re-reviews are a separate, independent mechanism.
- External agents: workspace-link authorize no longer tells a non-owner whether a link id exists; MCP `tools/list` is per agent (an external agent is never offered the task or helper tools); delegation budgets accept
  and enforce `max_turns` (an answer or proposal past it closes the task; a `complete` answer is always accepted, and a task stored without `max_turns` has no ceiling; and echo `max_hops`, the chain-depth ceiling); a consultation also records `asker -> target` as a graph edge (reference nodes).
- Acceptance: four Herdr strict xfails became real tests; the manifest minimums rise (s2 20, s5 15); no minimum is lowered.
- No migration, no environment variable. Rollback: revert the PR. Checked by loading rows of this stream into the previous build: it starts and serves normally and the rows do no harm, but it does NOT keep them out of its event feed (it lists them as ordinary events, each carrying only the head and request id); the stored rows stay in the log and are ignored again once this code is back.

### T16 security fixes, Herdr side (stacked on the T16 release-gate branch)
- Ingest is an allow-list of entity types; `collab`, `ci_*` and other adapter-owned streams and the server's session receipts can no longer be forged
  by an ingest key (H1, M1, M5). Foreign `collab` rows already in a database are quarantined at startup, and a corrupt collab log disables collaboration
  alone with an ERROR instead of stopping the control plane.
- A full collab log still revokes: creating a revocable thing pre-pays its revocation (cannot be drained by create/revoke cycles or presence flips); chat stops at 90 %, other writes at 95 %, revocations may use 100 % (H2).
- `collab-repair` (dry run by default) sets aside foreign `collab` rows without deleting; a corrupt log disables collaboration only and shows in `/api/v1/health` (H1).
- `replay_history: since_join` now also covers node/edge/listing reads and the insights/progress snapshots (M2, L2).
- All collab feature switches (`steering`, `contextual`, `insights`, `delegation_enabled`, `replay_history`) are hot and fail closed the same way,
  including under `--collab-only` (M3, M15). One consistency test enumerates them.
- `/api/v1/ingest/sources` is operator-only for other principals' sources (M4); gap state is partitioned per key (M6); trusted evidence writers are
  selected before the contribution cap (M7); the quorum route hides a head the reader cannot read (M9); the generic rate limiter keys clients behind a
  proxy by their forwarded address (M12); an open listener is no operator in a reserved namespace (L1).
- Test: the graph neighbourhood budget test counts rendered nodes instead of timing a wall clock.
- Ingest sequence-conflict 409 now says `reason: "sequence_conflict"` and `last_sequence` (the caller's own source only) so a restarted emitter can resume (Pi H6).
- Round 4: the revocation reserve counts every unrevoked join token and every member including expired guests (revoking those emits an event); repair/server
  locking works on filesystems without `flock` (lock-file fallback, never a startup crash), re-checks the inode after locking, and refuses when no lock can be proven;
  repair on a pre-0003 database says to migrate first; degraded operator reads answer 503.
- Round 3: safety events are idempotent (a repeated `revoke_agent`/`external_leave`/user decision commits nothing, so they cannot fill the log); the
  revocation reserve is recomputed from state; `collab-repair --apply` works on a real PostgreSQL (tid binding fixed, tested against a throwaway
  server), refuses to rewrite a log a live server holds, reports a gap tail it cannot fix by default, and also quarantines rows whose
  timestamps disagree; unauthenticated callers cannot see that collaboration is degraded; a shared-mission event written from another room is shown
  to the reader after their join.
- Round 5 (after merging main): main's panic switch, helper cancel and host Remove work on a nearly full log (`helpers_paused`, `helper_cancelled`,
  `egress_removed` are safety events; helper requests, host approvals/promotions and owner-listed hosts pre-pay their revocation); a degraded start
  writes nothing into the fabric graph and keeps unknown agent namespaces reserved; ingest still accepts the system-graph adapter's
  `repository`/`request`/`span` and refuses the review-gate prefixes; the `.lock` fallback records `host pid` (another host's lock is live, our own
  pid is stale, so a container restarting as PID 1 starts; caveat: a recreated container gets a new hostname, so its predecessor's lock must be
  removed by hand); `collab-repair` keeps the log's owner and mode. The since_join id map is capped at 1,000,000 ids (oldest evicted first, an
  evicted id is pre-join: fail closed), see docs/contracts/lineage-events.md.
- No migration, no environment variable. Rollback: revert the PR.

### T16 end-to-end acceptance harness
- New `tests/e2e` suite (scenarios S1-S7) driven by `scripts/run_acceptance.py` against real servers, a real Postgres and a fake `gh`.
  It is opt-in: plain `pytest` and `scripts/validate.sh` deselect everything marked `e2e`; the runner passes `-m e2e`.
  Local overrides (autospec binary, Pi checkout, Postgres bin dir) go in the git-ignored `tests/e2e/acceptance.local.yaml`; no environment variables.
- The runner's verdict guards reject non-strict or bare `xfail`, and report "nothing selected on the herdr side" (pytest exit 5)
  instead of a failure when `--only` picks Pi-only scenarios.
- Redaction: `author`, `authors`, `author_login` and `author_name` keys are no longer redacted by name (review quorum compares the
  author login, and a redacted value let an author approve their own PR). The `sk-` pattern now also matches after an ANSI colour
  code or `%3D` and keeps ids such as `task-sk-ab` intact.

### Attention inbox, evidence-based progress and optimization analytics (T15, stacked on #174, #173, #168, #169)
- New read routes `GET /rooms/{id}/attention|progress|analytics` and `POST /rooms/{id}/attention/ack|snooze|restore`, plus Attention and Progress
  tabs. Everything is derived from what the reader can see in the room graph, review quorum (T06), steering/promotion (T12/T13), proposals (T11),
  consultations (T14) and the external-agent roster; nothing is copied into a second store. Ack and snooze are per reader, bounded and bound to the
  state of the item, so a changed condition comes back.
- Completion counts only evidence a configured trusted writer attests (merged pull request, validated edge, accepted proposal); an agent saying "done"
  is shown but never counted. Estimates state their method, confidence and assumptions, or say "insufficient evidence".
- Analytics (cycle time, rework, re-review, approach win rate, blocked/idle time, self-reported cost) cover only visible items, hide groups smaller than
  `min_group_size`, and have no per-person or per-agent breakdown.
- New YAML `control_plane.collab.insights` (off by default, `kill_switch`). No environment variable, no migration, no Pi change.
- Rollback: `insights.enabled: false`, or revert the PR.

### Contextual chat, consultation and cross-workspace links (T14, stacked on T11 #172 and T12 #171)
- Chat messages can carry one validated reference to a graph node, edge, comment or proposal. It is checked when posted and again for every
  reader of every read; a reader who cannot see the target gets one fixed "hidden reference" and nothing else (agents, remote agents and guests always do).
- Consultation: a member asks another member or a remote agent one bounded, expiring question about a graph node and gets one text answer
  (never a task, proposal or action). The graph records who asked whom about what, never the text. New MCP tools `consult_inbox`, `consult_answer`.
- Cross-workspace links: new edge kind `cross_link`, authorized on both ends and written only by the HCN service for one room; revocable and
  self-expiring when the room loses a workspace grant. See `docs/contracts/lineage-events.md` ("T14").
- New YAML `control_plane.collab.contextual` (everything off by default, `kill_switch`). No environment variable, no migration.
- Rollback: `contextual.enabled: false`, or revert the PR.

### Alternative implementation forks, comparison and promotion (T13, stacked on T12)
- New steering actions `fork` and `promote` (opt-in via `steering.actions` and `steering.forks.enabled`), a comparison read model
  `GET /api/v1/collab/rooms/{id}/forks`, and a Forks panel in the room workbench. Fork preserves the original; nothing is deleted.
- Promotion is two-step and human-confirmed, promotes exactly one fork at an exact head, and never merges. See `docs/contracts/lineage-events.md` ("T13").
- Rollback: revert the PR (code only; no migration).

### Bounded task delegation and artifact proposals (T11, stacked on #170, #167, #165, #164)
- A room owner can delegate one bounded task (scope, budget, expiry, allowed output kinds) to a coworker's external agent invited with
  `proposals: true`. The agent answers only with an artifact proposal (patch, doc, analysis or link) that waits in quarantine; a room owner
  accepts or rejects it with the digest of what was reviewed. Accepting records `task:hcn-delegation.*` and `artifact:hcn-proposal.*` nodes and a
  `produced` edge in the lineage graph; nothing is ever applied. Same checks over REST, MCP (4 tools) and A2A (`tasks/get|list|cancel`, `message/send` with a `taskId`).
- New YAML kill switch `control_plane.collab.delegation_enabled` (default `false`; works for the combined and `--collab-only` servers). No environment variable.
- Extends the existing task store, event log and external credentials; new log events are additive; no migration. Defence in depth (untested): an API key whose configured
  role is spelled `external` or `collab` is given principal kind `unknown`, never those service-only kinds. See `docs/operations/bounded-delegation.md` and the T11 addendum of the HCN threat model.

- Hardening after independent review: bounded validator regexes, cheap checks before parsing and parsing outside the lock, strict `---`/`+++` names, `/dev/null` vs hunk
  agreement, wider hard-coded deny-list, CRLF refused, task-list `bounds` limited to owners, open ingest listener refused for the reserved node ids.
- Rollback: `delegation_enabled: false`, or revert the PR.

### Lineage graph, Gantt timeline and replay (T09, stacked on #167, #165, #164)
- Room workbench: new read-only "Lineage" panel (`packages/collab-lineage.js`): layered lineage graph, Gantt timeline with
  head-change and stale markers, replay scrubber, filters and node inspector, each with a table alternative; keyboard
  operable; nothing encoded by colour alone; all ids and labels escaped.
- New read routes `GET /api/v1/collab/graph/lineage[/timeline|/events]` (room_id required, same membership 404, budgets, 240/min per person).
  Replay at a position equals the live projection at that point and never shows what the reader cannot see now.
  No migration, no new table (derived from the stored lineage events). See `docs/contracts/lineage-events.md` ("T09").
- Replay is per room (a busy room cannot break another's), visibility is scanned in 128-event chunks so viewers do not stall
  ingest, member responses no longer carry the global `admission` counter, the ledger window is counted in the reader's own visible events (exact steps, opaque page tokens), and the new
  operator YAML `control_plane.collab.replay_history` (`since_join` default | `full`) sets how far back a member may replay.
- Rollback: revert the PR (code only).


### Head-bound review quorum (T06)
- Review round 4: head handling is conservative. A lower-sequence write after the applied one is ignored only if it repeats
  the applied head or a head that writer already reported earlier; any other head makes the PR `head_unverified`
  (`head_unverified`/`alert` fields, one log line, operator `resync_event_id`). It clears when the writer writes past the
  applied sequence or an operator acknowledges the write with `attrs.head_resync`; a run of writes is never auto-adopted
  (`head_rebased` is gone, `head_resynced` replaces it). Evidence is kept per distinct head so re-emits cannot evict it.
- Review round 3: `rereview_needed` counts only what the caller can see. Matching is ASCII-only (same as autospec;
  `invalid_target`). Shared vectors grew to 32 cases.
- `GET /api/v1/graph/nodes/{pr_id}/review-quorum`: derived, head-bound quorum over `reviewed` edges (stale verdicts never
  counted, distinct configured reviewers, author/replay/forged-head defences, `rereview_required`). Config:
  `control_plane.review_quorum` in the fleet config. One policy shared with `autospec review-quorum` via
  `tests/control_plane/fixtures/review-quorum-vectors.json`; statuses `unknown_head`, `head_unverified` (emitter sequence
  reset), `unknown_author`; digests are 64-hex and bound; `reviewer_groups`. Contract: `docs/contracts/lineage-events.md`. Rollback: remove the
  block (endpoint answers 404); no schema or event change.

### Tabs/panes and membership-scoped lineage (T08, stacked on #165 and #164)
- Lineage `room`/`mission` scopes now resolve to HCN membership: an event that names its `room_id` / `mission_id`
  (and optionally `workspace_id`) is readable by that room's non-guest members / that mission's listed members (T07),
  through the new `GET /api/v1/collab/graph/...` routes (node, neighborhood, edge and a per-room type listing);
  everyone else still gets 404, and a signed-in person never reads `org` lineage. Bare `room`/`mission` stay
  operator-only and the keyed `/api/v1/graph` API is unchanged. No migration; reads only.
- Room workbench: new read-only "Tabs" panel (`packages/collab-tabs.js`): primary agents as tabs, spawned subagents as
  panes, from recorded `spawn` lineage only. Closing a pane never changes the agent.
- Breaking: `GET /api/v1/collab/graph/nodes` (listing) requires `room_id` (400 otherwise); a key with no
  `control_plane.lineage_key_scopes` entry can no longer write `room`/`mission` scope (403 `lineage_forbidden`; `"*"` opts in);
  mission scope needs `room_id`, so stored mission events without it become operator-only after rebuild.
- Rollback: see `docs/contracts/lineage-events.md` ("T08 rollout and rollback").

### Plannotator bridge: digest echo
- `herdr-plannotator plan` now prints the `plan_digest` (and `review_id`) the receipt was bound to alongside the upstream decision, computed by the bridge and authoritative over upstream output, so a caller can verify a decision belongs to the exact plan bytes it submitted. Code reviews are unchanged.

### YAML-only fabric agent configuration
- The fabric agent, its install scripts, the enroll-sync/service-metadata units and the Ansible fabric tasks no longer
  write or read `fabric.env` (`HERDR_ENGINEERING_FABRIC_URL`). The gateway URL is `fabric.url` in the central
  `fleet-config.yaml` (fallback: `fabric.gateway_url` in `config.yaml`). `--gateway`/`--control-plane` on the install
  scripts are ignored with a warning. Existing `fabric.env` files are left in place and are harmless.

### Lineage graph projection (T02, Herdr side)
- Migration `0004_sequence_domain_per_principal.sql`: the envelope sequence domain is `(actor id, source)`.
- Migration `0005_event_id_per_actor.sql`: `event_id` is unique per principal (global primary key dropped).
- Lineage review round 6: commons fallback primary is the writer who wrote the field first (stable under updates) and
  readers apply the earliest VISIBLE owner; neighborhood stops rendering neighbours once its budget is spent;
  commons admission-limit errors are generic; known-limitations list in the lineage contract.
- Lineage review round 5: commons field visibility is per writer (a widened node no longer exposes another writer's
  private fields); an empty ensure never makes the ensurer the commons owner; commons endpoints confer no tombstone
  authority; tombstoned edge instances free their slot; contribution caps keep operator/owner/own entries; migration 0005
  rollout order and rollback documented (`docs/operations/event-id-per-actor-rollout.md`).
- Lineage review round 4: admission mode/sequence are server-only (reserved `_` payload keys rejected); commons nodes keep
  an owner/operator primary value with attributed `fields`/`contributions`, widest-scope visibility, per-writer attr budgets
  and per-writer `from`-commons edge instances; `node.state` on a missing commons node creates it.
- Lineage refusals are HTTP 403 `{"reason": "lineage_forbidden"}`; shared COMMONS nodes (commit/pr/issue/ci/artifact) and
  idempotent "ensure" upserts are accepted from any ingest key.
- `lineage` envelope events (v2 only) project into a durable graph derived from `fabric_events`
  (no new table; rebuilt on hydrate). See `docs/contracts/lineage-events.md`.
- `GET /api/v1/graph/nodes/{id}`, `.../neighborhood`, `/api/v1/graph/edge`: viewer-level reads,
  visibility-filtered at every hop, depth/node/edge capped, fail closed.

### Event envelope v2 (T01, Herdr side)
- `POST /api/v1/ingest` accepts `schema_version: 2` bodies per
  `docs/contracts/event-envelope-v2.md`: server-set `actor`/`received_at`,
  idempotent `event_id`, `(source, source_sequence)` collision and gap tracking,
  409 on conflicts, fail-closed `visibility_scope`. v1 bodies are unchanged.
  `GET /api/v1/ingest/sources` reports per-source sequence, gaps and freshness.
- Migration `0003_event_envelope_v2.sql` adds nullable envelope columns, a
  partial unique index and `fabric_event_sources`.
- `EventStore.append` no longer reports every error as a duplicate: only an
  existing `event_id` returns False; other failures propagate.

### Terminal button on agent pages
- An agent page has a **Terminal** button (operator only). It shows the exact command
  that attaches a terminal on the viewer's own machine to the agent's HerdR pane:
  `ssh -t <host> "bash -lc 'herdr --session <s> agent attach <pane>'"` from another machine,
  or `herdr --session <s> agent attach <pane>` on the agent's host, each with a Copy button.
  The server builds it from the recorded session only (strict charsets, never client input)
  and never runs it. `POST /api/v1/sessions/<id>/terminal`; see
  `docs/operations/terminal-attach.md`. The old "Copy attach command" line
  (`ssh host && herdr workspace focus ...`, which ran the second half locally) is gone
  from the page.

### Host telemetry and fleet provisioning
- Collector (`host_metrics.py`, run by the fabric agent): CPU, memory, swap, disk and
  mounts, load, uptime, network, GPU (NVIDIA, ROCm, Apple), top consumers and agent
  count every 30 s, plus an hourly report of package/security updates, reboot-required,
  failed units and tool versions. Intervals: `fabric.host_report_interval` and
  `fabric.host_slow_interval` (YAML only). The agent count is the number of agent
  processes named in `fabric.agent_binaries` (YAML, default `pi`, `claude`, `codex`).
- Control plane: validated host metrics with 48 h raw history and 7 d of durable
  5-minute rollups (max 300 points); `GET /api/v1/hosts/<id>/telemetry?range=1h|24h|7d`;
  server-side `problems`/`problem_count` on host reads (stale, never_reported, disk,
  memory, GPU, failed units, security updates, reboot, version drift, update age).
- Pages: host detail page and richer list cards with range picker, gauges,
  sparklines, GPU, mounts, top consumers, updates, versions with pin drift, a
  problems strip and a never-reported state.
- Ansible: `tailscale` role (joins only from `NeedsLogin`, auth key from Vault) and
  `fleet-update.yml` with the `herdr_fleet_update` role, run daily by cron on the main
  host: security-only OS updates, busy-host deferral, pinned herdr-eng revision, one
  host at a time, agent restart once, `maintenance` flag and a forced single-host run.
  See `ansible/README.md`.
- Behaviour changes: `GET /api/v1/hosts/<id>/telemetry?limit=` is now honoured
  (1..500); host events from non-agent sources (e.g. the fleet update) no longer
  refresh a host's liveness or create hosts; `versions.herdr` is now the herdr binary
  version and `herdr_eng` is the herdr-engineering package version.

### Central fleet config: deploy and servers
- `deploy/control-plane/deploy.sh` renders the compose variables from the central
  fleet file into a transient 0600 env file (removed on exit, even on failure),
  mounts the file's own directory read-only at `/etc/herdr/fleet/` and refuses to
  run when the file is missing or invalid or its directory holds anything else. `generate-env.sh`, `.env.example` and the
  Woodpecker/GitHub token-file compose overrides are removed.
- The control-plane web, worker and llm-proxy read configuration only from the
  fleet file (`--fleet-config`): Postgres DSN, host aliases, RustFS/MinIO,
  `control_plane.ci_actions`, `control_plane.inspector_limits`,
  `control_plane.llm_proxy`, `llm.upstream_key`. The `HERDR_CP_*`,
  `HERDR_INSPECTOR_*`, `HERDR_LLM_PROXY_*`, `HERDR_ENGINEERING_CI_ACTIONS` and
  `FABRIC_LLM_UPSTREAM_KEY(_FILE)` variables no longer have any effect.
- `deploy-fleet-agents.sh` copies no env or secrets and skips hosts without `host.json`.
- The central file lives alone in `~/.config/herdr-engineering/fleet/` (init leaves a
  symlink at the old `~/.config/herdr-engineering/fleet-config.yaml`). Writers replace
  it atomically (rename), dated backups go to `fleet-backups/` (newest 10 kept), and
  `herdr-eng fleet config revoke <host>` removes a host. Rewriting drops YAML comments.

### Collaboration network foundation (HCN P1, flag off by default)
- Added `control_plane/collab.py` and `/api/v1/collab/*`: per-user bearer auth,
  two-user rooms with owner/contributor/viewer/guest roles, device-bound agent
  enrollment with revocable, rotating credentials, owner-attenuated agent grants,
  idempotent auditable messages, cursor replay and a per-room SSE that
  re-authorizes every tick. State is event-sourced into the existing
  `fabric_events` sink and kept out of the shared event streams.
- Enable with `control-plane web --collab-users-file`; see
  `docs/operations/collaboration-network.md` and ADR-0007.

### Fleet mission recovery
- Added pinned Pi runtime staging and `fleet rollout plan/apply/run/status` with
  durable operation IDs, offline retries, bounded online stall detection, loaded
  build checks and preservation of operator pauses.
- Native discovery and control share the frozen target workstation. Missing
  conversation references can be verified against the foreground PID and live
  private Pi control instance without reading transcripts.
- Fleet parity now requires complete inventory coverage and installed provenance;
  macOS companion installation preserves the selected release.
- Session messages use real native delivery with durable receipts. Unsupported
  lifecycle actions and delivery without durable storage fail explicitly.

### Added
- `herdr-eng session run --project P -- <cmd>` injects `OPENAI_BASE_URL` / `OPENAI_API_KEY` (a
  per-session or reused per-project key) plus service-URL hints into a session's environment,
  revokes per-session keys at exit, and has opt-outs (flag, `llm.inject`, env). `llm-env` now writes
  its file atomically at 0600 and never follows redirects.
- The fabric agent warns once per port that listens like a dev service but has no lease, and
  `scan_once` returns them as `unleased`.

### Security
- **Fixed an authorization bypass in the control-plane API.** `_require` sent the 401/403
  response but returned `None`, so the handler then executed the request anyway: with API keys
  configured, a viewer key could set preferences / post session messages / run session actions,
  and an unauthenticated caller could register hosts via `/api/v1/ingest`. Refusals now stop the
  request. Deployments that rely on keys should upgrade; `--allow-open` (tailnet-trust) deployments
  were never affected because they have no keys to enforce. Regression test:
  `test_refused_requests_do_not_execute`. Refusals also close the connection so an unread
  request body can no longer be parsed as the next request.
- **Behaviour change (keyed mode only):** because refused GETs used to run anyway, the static UI,
  `/api/v1/health` and the SSE streams worked with no key. They now correctly return 401, and the
  bundled UI (which sends no `X-API-Key`) cannot load against a keyed server. Tailnet-trust
  (`--allow-open`) deployments are unaffected.

### Docs
- Inspector authentication: tokenless (tailnet-trust) vs keyed operation, and the privacy
  implications of open mode (spec 0200).

### Added
- Agent inspector (spec 0200, ADR-0006): per-session conversation / reasoning /
  tools / files / tests / usage views, streaming tool status, searchable
  history, diffs, subagent attribution, mission/handoff/worktree/PR
  associations, per-agent & global preferences, bounded retention, role-gated
  and redacted bodies. Mounted inside the existing session view.
- `agent-inspector-event` contract; `entity_type=inspector` on `POST /api/v1/ingest`.

### Fixed
- Session detail page requested the wrong session id (`parts[1]`) and 404'd.
- Control-plane listen backlog raised from 5 to 256 (connection resets under bursts of agents).

### Added
- **Operability of the live control plane**:
  - `/api/v1/health` now reports accurate deployment context — `source`
    (`postgres` when hydrated from durable state, else `memory`), image
    `version`, and a build-time `commit` stamp (Dockerfile `GIT_COMMIT` build
    arg / `HERDR_ENGINEERING_COMMIT` env; `dev` for local builds).
  - Lightweight per-client **rate limiting** on `/api/v1` (in-memory
    sliding window, generous default, SSE streams excluded) with `429` +
    `Retry-After`.
  - **Redaction** extended: `client_secret`/`authorization`/`x-api-key`/`auth`
    keys, `dsn`/`connection_string` key hints, OpenSSH private-key blocks, and
    more connection-string schemes (`postgresql`, `rediss`, `mongodb+srv`,
    `amqp`, `minio`, ...).
  - README documents the TLS path (`tailscale serve --https` on bender) for
    exposure beyond the Tailnet.

### Added
- **Live deploy of the control-plane compose stack on bender** — the HerdR UI
  is now served at `http://dev.lan` (port 80) on the Tailnet IP, backed by
  durable Postgres + MinIO. Compose now reads `HERDR_CP_PG_DSN` (generated into
  `.env`) so the web/worker reach Postgres by service name over the bridge
  network; MinIO is pinned to a known-good RELEASE tag (minio:latest is not
  always pullable); the worker takes `--migrations /app/migrations` because the
  bundled migrations path doesn't exist inside the installed image.

### Fixed
- **Web/worker durable hydration** (found during live deploy):
  - `_maybe_pg_source` imported `from .bridge` (wrong module) — now
    `from .control_plane.bridge`, and it reads `HERDR_CP_PG_DSN` from the env so
    the DB password never appears in process args.
  - asyncpg connections cannot be used across separate `asyncio.run` calls —
    `PostgresSource` now owns a single persistent event loop (`_LoopRunner`,
    daemon thread) and lazily connects + fetches on it.
  - Migration `0001_control_plane.sql` had a `UNIQUE (...)` constraint
    containing `COALESCE(...)` expressions (Postgres forbids expressions in
    constraints) — replaced with a `CREATE UNIQUE INDEX`.

### Added
- **Dev Fabric — port-matching (localhost:<port> -> dev.lan:<port>)** — a
  locally started dev server now appears at the SAME port on `dev.lan`, not a
  random range port:
  - Agent `AgentForwarder` tries binding the same port on the tailnet IP first
    (falling back to an OS-allocated ephemeral port if that exact tailnet port
    is taken), so loopback-only services register `(tailnet_ip, <same port>)`.
  - Gateway `register()`/`_allocate_port()` prefer `preferred_port` (defaults to
    `target_port`) whenever it is bindable and collision-free — falling back to
    the `[18000, 28999]` window otherwise. Preferred ports outside the window
    (4040, 5173, 3000, ...) are honored. Idempotent re-registration still
    preserves the existing external port across heartbeats.
  - The gateway front-door feedback-loop guard is narrowed: a port is skipped
    only when the local listener is already bound to the tailnet IP / all
    interfaces (i.e. it is the router front-door or a 0.0.0.0 service already
    reachable on the tailnet). A loopback-only dev server on a port that
    another host also uses is still adopted (with its own forwarder + fallback
    external port), so two developers can both run port 4040 on different
    machines without one being silently dropped.

### Added
- **Control Plane — Phase 2 Shared Design System (spec 0190 §6-16, §92)** — new
  build-free `packages/` workspace shared by every HerdR web surface:
  - `packages/herdr-design-system/` — canonical **light** theme (ChatGPT-inspired,
    not dark-NOC) as plain CSS custom properties: surfaces, text, brand accent,
    semantic status colors, Inter/JetBrains Mono font roles, radius/shadow/
    motion tokens, plus `base.css` (reset, visible focus ring, reduced-motion).
    Status is never color-alone — every token pairs with a label/icon.
  - `packages/herdr-web-components/` — framework-agnostic Web Components
    (`<herdr-status>`, `<herdr-spinner>`, `<herdr-empty>`, `<herdr-error>`,
    `<herdr-ident>`, `<herdr-metric>`) with DOM-free pure modules
    (`status.js`, `format.js`, `util.js`) unit-tested via `node --test`.
  - `packages/herdr-web-client/` — structured `/api/v1` `HerdrClient` (missions,
    sessions, services, hosts, activity, analytics, message send, actions,
    health) + SSE `connectEvents()` live-update helper; injectable fetch/
    EventSource for node tests.
- **Control Plane — web UI (Phases 3-7, spec 0190 §5, §20-34, §42-47)** —
  `packages/index.html` + `packages/app.js`: a hash-routed SPA that is a pure
  **client** of the structured API. Implements the full route inventory
  (`/overview /missions /missions/:id /herd /herd/session/:id /services
  /services/:id /hosts /hosts/:id /activity /analytics[/*]`), rendering
  loading/empty/error/data states via the design-system Web Components
  (`<herdr-app-shell>` sidebar shell, status badges, metric tiles, idents).
  Live updates are SSE-driven (not polling).
- **Control Plane — Artifacts API (Phases 5, spec 0190 §56-74, §102)** —
  `POST /api/v1/artifacts` (raw-body upload → stable `artifact_id`, SHA-256,
  mime, session/mission binding) and `GET /api/v1/artifacts/{id}/download`
  (streams exact bytes with `Content-Type` + `X-Content-Type-Options: nosniff`).
  Bytes held in the in-memory repo as the RustFS stand-in; the client is
  content-addressed and swap-to-RustFS is config-only (Phase 1 ruling).
- **Control Plane — Host telemetry (Phase 6, spec 0190 §23-25, §89, §105)** —
  `POST/GET /api/v1/hosts/{id}/telemetry` recording CPU/mem/disk/network/GPU
  samples per host, plus existing `/api/v1/leases` + `/api/v1/health`
  (control-plane self-observability).
- **Control Plane — Hardening (Phase 8, spec 0190 §87-88, §96-104, §106)** —
  - `control_plane/redact.py` — boundary secret redaction (OpenAI/AWS keys,
    JWTs/Bearer tokens, passwords/secrets/API keys, private keys, DB
    connection strings); applied to event payloads in API/SSE responses and to
    session messages before persistence (secrets never stored/rendered).
  - `control_plane/retention.py` — TTL-based pruning of high-volume telemetry
    + old raw event frames + artifact bytes; structured entity records are
    durable and never auto-deleted (Spec §96, §99).
  - Backup/restore — `repo.export_state()` / `import_state()` JSON round-trip
    (Spec §102) with a restore test.
  - Role-based auth — `api_keys={key: role}` with `viewer` (read-only) /
    `operator` (read + mutate); `X-API-Key`; unknown key → 401, viewer POST
    → 403 (Spec §104).
- **Control Plane — Tailnet trust boundary + .env secrets** —
  - **Auth-free within dev.lan**: the compose web service no longer needs API
    keys. The Tailnet is the trust boundary (all machines belong to each other,
    WireGuard-encrypted); `--allow-open` is the explicit acknowledgment. The
    web host port binds to the Tailnet interface via `HERDR_CP_WEB_BIND`
    (default `127.0.0.1`), so it is reachable on dev.lan but not from `0.0.0.0`.
  - **`.env` with random secrets**: `./generate-env.sh` writes a git-ignored
    `deploy/control-plane/.env` (0600) with randomly generated strong Postgres
    + MinIO passwords; compose refuses to start without them and DB/object-store
    ports stay loopback-only. `.env.example` documents every variable.
  - **Fail-closed auth kept for standalone**: an unauthenticated standalone
    server still refuses to bind a non-loopback address unless `--allow-open`
    is passed; `X-API-Key` compare stays constant-time (`secrets.compare_digest`).
- **Control Plane — security hardening (fail-closed auth, constant-time, sealed compose)** —
  - **Fail-closed auth (Spec §104)**: an unauthenticated server refuses to bind
    any non-loopback address (raises `ValueError`); loopback stays open for
    local dev only.
  - **Constant-time key compare**: `X-API-Key` verification uses
    `secrets.compare_digest` instead of `==` (removes a timing side channel).
  - **Sealed compose defaults**: Postgres and MinIO credentials are no longer
    hardcoded `herdr:herdr`; compose refuses to start without secrets, and
    Postgres (5432) / MinIO (9000/9001) publish to `127.0.0.1` only.
- **Control Plane — live SSE wiring + per-entity event stream + demo seed** —
  - The SPA now subscribes to `/api/v1/events/stream` and refreshes the current
    view on incoming events (debounced, no polling) — live interaction (Spec
    §52, §85) is wired in the browser, not just available client-side.
  - New `GET /api/v1/events?entity_type=&entity_id=` returns a single entity's
    full event stream (previously the UI filtered the global 20-event window).
  - Fixed host-telemetry route shadowing: `/api/v1/hosts/{id}/telemetry` was
    being swallowed by the host-detail branch (`len(parts) > 4`); the detail
    branch is now exact (`len(parts) == 5`). The host view now shows the latest
    CPU/mem/disk sample instead of always-empty fields.
  - Removed the duplicated `esc()` in `app.js` (now imports `escapeHtml`).
  - `herdr-eng control-plane web --seed-demo` seeds a fresh in-memory repo so
    the UI is populated for local evaluation when no Postgres is present.
- **Control Plane — Durable bridge (spec 0190 §106, §114-115)** —
  `control_plane/bridge.py`: `load_into(repo, source)` hydrates the in-memory
  repo from a read-only durable view (Phase-1 Postgres tables: missions,
  sessions, services, hosts, fabric_events), so the API/UI can serve real
  persisted state. `PostgresSource` is asyncpg-backed (lazy, graceful
  degradation if asyncpg is absent); the mapping is pure and unit-tested with
  fakes (no live DB in CI). `herdr-eng control-plane web --pg-dsn <dsn>`
  hydrates from Postgres on startup.
- **Control Plane — `control-plane-web` compose service + `--api-keys` RBAC** —
  compose project now ships a web service (UI + `/api/v1` + SSE, private by
  default, `127.0.0.1:8080:8080`), and `herdr-eng control-plane web` accepts
  `--api-keys 'k1=viewer,k2=operator'` (or `HERDR_CP_API_KEYS` env) for
  role-based `X-API-Key` auth.
- **Control Plane — `herdr-eng control-plane web`** subcommand — serves the UI +
  `/api/v1` + SSE private-by-default (`--host 127.0.0.1`), optional `--api-key`,
  from the bundled `packages/` web root.
- **Control Plane — structured repository + API/SSE server (spec 0190 §20-34,
  §42-47, §52, §85, §91-92)** — the UI-is-a-client backbone:
  - `control_plane/repo.py` — in-memory, event-sourced `ControlPlaneRepo`
    (missions/sessions/services/hosts/artifacts/leases) with stable identity,
    dynamic location, 0-indexed per-stream event sequencing, replayable
    `recent_events`, pub/sub `subscribe()`, and mission/model/host analytics.
  - `control_plane/api.py` — stdlib `ControlPlaneServer` exposing structured
    `GET/POST /api/v1/*` JSON + **SSE** live stream (with initial frame + history
    replay + auto-reconnect), optional `X-API-Key` auth, and static serving of
    the design-system/web packages. Private-by-default (loopback/tailnet),
    daemon handler threads (a stuck SSE client can never block shutdown).

- **HerdR Dev Fabric Control Plane — Phase 1 Foundation (spec 0190)** — new
  `herdr_engineering/control_plane/` subpackage laying the structured-state
  backbone for the unified `http://dev.lan` control plane:
  - **Stable entity IDs** (`ids.py`) — time-ordered, lexicographically sortable
    Crockford-base32 ULID-style ids (`new_id(kind)` for mission/session/
    service/artifact/host/agent/plan/worktree) with a per-process monotonic
    counter so creation order matches string order even within the same
    millisecond. Identity is stable; location is dynamic.
  - **Versioned SQL migration runner** (`db.py`) — homegrown idempotent
    `Migrator` (`ensure_schema`/`applied`/`apply`, `schema_migrations` table,
    per-file transactions, filename order).
  - **Phase-1 schema** — `deploy/control-plane/migrations/0001_control_plane.sql`
    with all Spec §53 tables (hosts, host_samples, missions, mission_events,
    mission_stage_runs, plans, plan_revisions, sessions, session_events, agents,
    agent_runs, services, service_events, artifacts, artifact_bindings,
    artifact_materializations, token_usage, resource_usage, test_runs,
    test_results, reviews, review_findings, pull_requests, audit_events,
    fabric_events, state_snapshots) + indexes.
  - **Event envelope / sequencing / idempotent ingestion** (`events.py`) —
    `EventStore` with per-stream 0-indexed sequences, exclusive resumable
    `read_stream(after)`, and `INSERT ... ON CONFLICT DO NOTHING RETURNING`
    so a duplicate `event_id` is ignored (idempotency enforced by the PK).
  - **Mission/Session/Service stores** (`models.py`) — stable-id CRUD with
    provenance fields.
  - **State snapshots + replay-after** (`snapshots.py`) — `SnapshotStore.save`/
    `load_latest` and `recover()` folding later event payloads over state, so
    no full event replay is needed after restart.
  - **Content-addressed RustFS client** (`rustfs.py`) — `content_key` =
    `sha256/ab/cd/<hex>`, `put`/`get`/`head`/`delete`, `IntegrityError` on
    SHA-256 mismatch. boto3/S3-agnostic (MinIO drop-in for Phase 1).
  - **Artifact model** (`artifacts.py`) — `ArtifactStore.register`/`download`/
    `attach`/`materialize`/`mark_available`; artifacts are content-addressed,
    never stored in Postgres, integrity-verified on download.
  - **Bounded background worker** (`worker.py`) — `run_one_pass`
    (stale-lease detection + host health reconciliation + snapshots + artifact
    GC hook; hosts/services marked unavailable, never deleted) and `run_worker`
    (infinite loop with `stop` event that never raises).
  - **Compose project** — `deploy/control-plane/` with `postgres:16-alpine`,
    `rustfs` (MinIO drop-in), and `control-plane-worker` (runs migrations then
    the worker; separate from the web frontend).
  - **CLI** — `herdr-eng control-plane migrate|worker` subcommands.
- **Multi-operator fleet lifecycle** — idempotent scripts + guide so any
  operator can stand up a private dev net, join a machine, and leave one:
  `scripts/fabric-agent-macos.sh` (join a macOS host via `uv` + Python 3.12
  venv + launchd LaunchAgent; subcommands `install`/`status`/`uninstall
  [--purge]`), `scripts/fabric-offboard.sh` (cross-platform leave: stop the
  local agent — systemd on Linux / launchd on macOS — close the host's leases
  on the gateway via its tailnet IPv4, and drop it from the fleet inventory;
  flags `--host`/`--gateway`/`--leave-tailnet`/`--purge`), and
  `docs/operations/fleet-lifecycle.md` (set up → join → verify → leave,
  including the one external Tailscale split-DNS step). README and
  `deploy/fabric-stack/README.md` now point at this flow.
- **`dev.lan` shared dev domain (Option B) + master-host compose stack** —
  `deploy/fabric-stack/` runs the whole fleet front door as one Docker Compose
  project on the gateway host: CoreDNS (authoritative for the `dev.lan`
  namespace, split-DNS from Tailscale), the fabric gateway (`url_base:
  dev.lan`, so every lease is `http://dev.lan:<port>`), and the private web
  UI. All services use `network_mode: host`; CoreDNS binds only loopback + the
  tailnet IP (avoids systemd-resolved/libvirt on port 53). The per-host
  auto-registration agent stays a host systemd unit. Deploy via
  `scripts/fabric-stack.sh` (generates `.env` + CoreDNS config from the live
  tailnet IP, builds the image, replaces the old host-run gateway/web).
  `.local` is mDNS-reserved, so Tailscale MagicDNS will not serve it — the
  admin-console split-DNS nameserver (restrict to `dev.lan`) is the one
  external step. See `deploy/fabric-stack/README.md`.
- **Gateway container-safe lock handling** — `devfabric gateway` now handles
  SIGTERM (graceful stop releases `gateway.lock`, so `docker compose stop`/
  restart no longer leaves a stale lock) and gains `--reset-lock` to take
  over a stale lock after a hard crash/SIGKILL. This is required in a
  container, where the entrypoint is always PID 1 and a stale lock from a
  previous container (`pid=1`) was otherwise mistaken for a live instance and
  permanently blocked restart. The compose stack passes `--reset-lock`.
- **Dev fabric gateway** — the unified `dev:<port>` front door (spec 0110,
  component #3). `herdr-eng devfabric gateway` runs the fleet gateway:
  control API (register/heartbeat/close/list/resolve/healthz) on
  tailnet+loopback with optional bearer token, a dynamic router that binds
  each leased external port on loopback + tailnet and byte-proxies to the
  owning machine, and a probe loop that renews healthy leases, expires dead
  ones (TTL), and releases their ports. Every lease gets a stable fleet-wide
  address `http://<gateway-magicdns>:<port>` (e.g.
  `http://bender.<tailnet>.ts.net:18000`) reachable from any tailnet device.
- `HERDR_ENGINEERING_FABRIC_URL` / `fabric.gateway_url` config: registrar
  commands route through the gateway automatically and degrade to local mode
  with an explicit warning when it is unreachable.
- **Dev-fabric auto-registration agent** (spec 0110, zero manual steps) —
  `herdr-eng devfabric agent` runs a per-host loop that detects web services
  starting locally and registers them with the gateway automatically: it
  scans listeners (`ss`/`lsof`), probes each for HTTP, puts a tailnet-bound
  forwarder in front of loopback-only apps, heartbeats tracked leases and
  transparently re-registers any the gateway drops (restart/TTL), and closes
  leases when a service disappears. A noise filter registers only *dev* web
  services (skips privileged/ephemeral ports and well-known infrastructure
  daemons; thresholds configurable via `fabric.agent_*`). Deployed as a
  systemd user service by `scripts/fabric-agent.sh` and the fleet Ansible
  playbook on every host, so a web service started on any fleet member
  appears at a stable `dev:<port>` and in the gateway host's web UI with no
  manual registration.
- The web UI's dev tab now sources `url_base` from the live gateway
  (`/healthz`) and renders a clickable front-door link per lease.
- `--host auto` for `devfabric register`: resolves the local tailnet IPv4 and
  verifies the target is listening before publishing the lease.
- `gateway.lock` (pid:nonce): refuses a second live gateway over one store;
  stale locks from dead PIDs are taken over.
- Fabric gateway tests (register/route, close/unbind, dead-target expiry,
  healthy renewal, token auth, list/resolve, restart re-bind, port-taken
  refusal, URL resolution, single-gateway lock, API shape).
- `docs/architecture/dev-fabric-gateway.md` (design + live evidence).
- **Live Woodpecker CI view** — validated against the real instance
  (Woodpecker 3.18.1). `herdr-eng ci repos|agents|pipelines|pipeline|logs|
  debug`: repos, runner agents (auth token stripped), pipelines with status,
  pipeline→task drill-down (3.x `workflows[].children[]` flattened to tasks
  with state + exit code), honest step-log handling, and a bounded
  Debug-with-Pi handoff for failing pipelines.
- **Real Ansible convergence of three of four fleet hosts** — `mac` (macOS,
  over the tailnet), `beast` (Ubuntu 24.04, over Tailscale SSH) and `bender`
  (local): each converged then immediately idempotent (`changed=0`).
  Inventory renamed `macbook-m4` → `mac` to match the real tailnet name;
  per-host connection details moved to gitignored `inventory/host_vars/`.
  `fry` is not on the tailnet (blocked for that host alone).
- Ansible fixes found during the live runs: brew check gated on a
  `brew_present` fact (the old check always exited 0, so brew tasks would
  fail on a Mac), `become` overridable (`-e herdr_engineering_become=false`)
  for user-scoped hosts without passwordless sudo, play PATH now includes
  the Homebrew prefixes and the fleet user's `~/.local/bin` (non-interactive
  SSH on macOS does not source the login PATH — and the setup-task
  interaction made herdr checks fail on every run), the tailscale report
  no longer matches a word that `tailscale status` never prints, and the
  config `lineinfile` now sets `owner` — with `become: true` it created the
  fleet user's config root-owned and unreadable by that user (caught by the
  idempotency re-run on beast, which also repaired the live file).

### Fixed
- **Fabric agent could hang forever on a wedged DNS resolver** (root cause of
  a long-run live stall): `urllib`'s `timeout=` covers the TCP connect/read
  but NOT `getaddrinfo`, so a control-plane call to the hostname `dev.lan`
  could block in the libc resolver indefinitely — no logs, low CPU, single
  thread, no new services adopted, fixed only by a restart. The `FabricClient`
  now resolves the gateway host to an IPv4 with a bounded lookup (daemon
  worker + `join(timeout)`, cached and refreshed), so a stalled resolver
  surfaces as a normal bounded `UnreachableError` the agent retries next
  cycle. As a safety net, `FabricAgent.run()` now runs a watchdog that, if a
  scan cycle fails to complete within `cycle_timeout` (default 300s), dumps
  every thread's stack to the log and exits so systemd/launchd respawn a
  healthy agent.
- **CI provider corrected to the real Woodpecker 3.x API** (found during live
  validation): base is `/api/`, not `/api/v0/` (the earlier `/v0` was a
  mis-diagnosis caused by trailing-slash 301s returning the SPA); repos are
  addressed by numeric id (resolved from `owner/name`); pipeline detail uses
  `workflows[].children[]` (tasks), not a `steps` array; offline `pipelines`/
  `pipeline`/`step_log` return empty/None instead of raising.
- `ci logs` no longer implies logs are missing due to a bug: it reports
  explicitly that this Woodpecker 3.x build streams logs over WebSocket (no
  REST endpoint) and the OAuth2 proxy does not forward a bearer token on the
  upgrade, then links the web UI.
- Fabric router `unbind` leaked one connection through an in-flight
  `accept()`; unbind now joins the accept threads so a port is only reported
  released when it truly is.
- The probe loop never released ports for expired leases (expiry was
  consumed by `active_leases()`' internal reconcile); the router now
  self-syncs to the active set each cycle (self-healing after restarts and
  closes).
- `gateway_url_from_config` ignored the `HERDR_ENGINEERING_FABRIC_URL`
  environment variable, silently degrading registrar commands to local mode.
- Registry store writes from an overlapping process could be lost on the
  next persist; the gateway now reloads the store when its mtime changes.
- Test port ranges were host-dependent; allocation tests now pick
  OS-free ports so they are hermetic.

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
