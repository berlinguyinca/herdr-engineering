# Fleet Provisioning, Base Project: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Anyone can stand the herdr system up from nothing on one machine, grow it to several, update it and roll it back with `herdr-fleet apply`; the owner's own fleet is adopted onto that path and the old install scripts and `ansible/` retire.

**Architecture:** The engine and roles live in the `herdr-fleet` repo (Python CLI that runs a packaged Ansible playbook; pins in `fleet.lock`, hosts and roles in `fleet-config.yaml`). Each capability is one Ansible role added with the recipe in that repo's `docs/adding-a-role.md`. Services run as pinned containers (digests); herdr, the host agents and the Pi toolchain are pinned native installs. An operator repo made by `herdr-fleet init` holds the operator's hosts, config, secrets and pins and names a fork per repo where wanted. Work that belongs to the product (image publishing, resource capture, retiring `herdr-web`) is done in `herdr-engineering`.

**Tech Stack:** Python 3.12, Ansible (packaged playbook, `fleet_json` callback), JSON-schema validated YAML, Docker compose for the containerized roles, systemd user units and launchd for native services, pytest plus the container scenarios in `tests/containers/`, Woodpecker and GitHub Actions CI.

**Spec:** `docs/superpowers/specs/2026-11-03-fleet-provisioning-design.md` (owner-approved), its backlog `2026-11-03-fleet-provisioning-backlog.md`, and `herdr-fleet/docs/specs/0001-declarative-fleet-provisioning.md`. Repos named below as `[fleet]` = `berlinguyinca/herdr-fleet`, `[eng]` = `berlinguyinca/herdr-engineering`. Tasks are done in a working checkout of the named repo, never in the other.

## Global Constraints

- One update path: nothing changes software on a host except `herdr-fleet apply`; no role gets its own script or timer (spec 0001). Old scripts retire only at parity (Task 21).
- No operator-specific value in any public repo: no host name, address, user, key, token, account or private-repo URL. Operator values live only in the operator repo; secrets only in vault-encrypted files or the operator's keystore.
- A pin names its source repository and exact commit; the default source is our public repo; a fork is a one-line change in the pins. Roles take URL and commit only from extra-vars built by `herdr_fleet/inventory.py:build_extra_vars`; no role hard-codes a repo URL, version, host name or image tag. Pins reject ranges, branches, `latest`, short SHAs and tag-only images (existing schema rules).
- Roles are idempotent, support `--check`, report a fingerprint through the `fleet_json` callback, and have a typed fingerprint plus a unit-tested `compare` (recipe steps 8 and 9). Schema `version` stays 1: new fields are optional with safe defaults.
- Services and timers on the control host are containers pinned by digest; herdr, host agents, the Pi toolchain, and everything on the Mac are native. No role assumes root beyond what its level needs; no role assumes the absence of a scheduler.
- Provisioning never creates or handles agent logins; it installs the CLIs and `doctor` reports login state.
- Backup targets must leave the host; restore is tested in CI.
- Container images are published by our CI to a public registry by digest; the `memory` role is optional; no repository is made public without the owner naming it. Never run `apply` against the owner's real hosts without the owner's explicit go-ahead for that host.
- Tests are RED first. TDD per the repo's rules ("No mocks; use the real example files", `docs/testing.md`). Match surrounding style and comment density. `scripts/ci.sh` passes before every PR.

## Review Focus

- A fork named for repo X is used end to end (clone URL, commit check, fingerprint) and a typo in the URL fails loudly before any host changes.
- `init` output validates, plans, and contains no real names; running `init` twice never overwrites an operator's existing files.
- A second `apply` changes nothing on a converged host, including on a host adopted from hand-made state.
- Adoption never deletes or rewrites existing data or volumes; a failed cutover of one service leaves the others running and is reversible.
- Backups taken by the system restore on a clean machine to a working control plane (not merely "files exist").
- Rollback by applying the previous `fleet.lock` restores prior versions without leaving the new ones running.
- The Mac and a no-root host never receive a step that needs root or Docker.
- A public-repo scan fails on a planted host name, IP address or token.

---

## Slice 1: foundation `[fleet]`

### Task 0: Working checkout and green baseline

**Files:** none created in the repo; a local checkout at `/Users/wohlgemuth/IdeaProjects/herdr-fleet` (clone of `berlinguyinca/herdr-fleet`).

- [ ] **Step 1:** clone, create branch `feat/base-roles`, create the venv per the README (`uv venv && uv pip install -e '.[dev]'`).
- [ ] **Step 2:** run `scripts/ci.sh`; record the result and the time it takes in the task report. If the container scenarios cannot run locally, record which part runs only in CI.
- [ ] **Step 3:** read `docs/testing.md` and `docs/adding-a-role.md` and list in the report any step that is out of date against the code.
- [ ] **Step 4: Commit** nothing; report only.

### Task 1: Repos pin a source URL and a commit

**Files:**
- Modify: `herdr_fleet/schemas/fleet-lock.schema.json`, `herdr_fleet/config.py`, `herdr_fleet/inventory.py` (`build_extra_vars`), `examples/fleet.lock.example.yaml`, `tests/test_lock_schema.py`, `tests/test_inventory.py`, `docs/specs/0001-declarative-fleet-provisioning.md` (one paragraph)
- Test: the two test files above plus `tests/test_loader_errors.py`

**Interfaces:**
- Produces: in `fleet.lock`, each entry of `repos` may be a bare commit string (today's form, source defaults to `https://github.com/berlinguyinca/<name>`) or `{url: <https url>, commit: <sha>}`; `repos` accepts additional names beyond the three required (`additionalProperties` becomes the same two forms). `build_extra_vars` emits `repo_urls` (name to URL) and `repo_commits` (name to sha) for roles. The loader rejects a URL that is not `https://` or `ssh://git@`, has credentials in it, or has whitespace; the message names file, key and remedy.

- [ ] **Step 1: Failing tests:** a bare string still validates and yields the default URL; the object form validates; a fork URL reaches `repo_urls`; a URL with `user:pass@` is rejected naming the key; a short SHA is rejected in both forms; an unknown repo name is accepted; the example file validates.
- [ ] **Step 2: Run to verify they fail** (`pytest tests/test_lock_schema.py tests/test_inventory.py -q`).
- [ ] **Step 3: Implement** the schema `oneOf`, the URL rules in `config.py` and the extra-vars.
- [ ] **Step 4: Run to verify they pass**, then `scripts/ci.sh`.
- [ ] **Step 5: Commit** `"fleet.lock: repos pin a source URL and a commit (forks)"`.

### Task 2: `herdr-fleet init`

**Files:**
- Create: `herdr_fleet/init.py`, `herdr_fleet/templates/` (operator repo templates: `fleet.lock`, `fleet-config.yaml`, `.gitignore`, `README.md`, `secrets/README.md`, `.github/workflows/plan.yml`), `tests/test_init.py`
- Modify: `herdr_fleet/cli.py` (new `init` subcommand)

**Interfaces:**
- Produces: `herdr-fleet init <dir> [--level 1] [--host-name NAME] [--user USER] [--address ADDR]` writes a ready operator repo: a lock with the current public pins (taken from `examples/` kept current by the release process), a one-host level-1 config, a `.gitignore` that excludes `secrets/` plaintext and `*.vault-pass`, and a README that states the next three commands. Refuses to write over an existing file (exit 2, lists the files); `--force` is not provided. `init` itself never touches the network and never needs a host.

- [ ] **Step 1: Failing tests:** the output validates with `herdr-fleet validate` and plans; contains none of the strings of a denylist built from the test's own fake operator values; a second `init` into the same directory fails and changes nothing; `--level` other than 1 is rejected with the message "level 2 is added by editing fleet-config.yaml: see README"; the templates are packaged as data (wheel check).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** `init.py` and the CLI wiring; add the templates to package data in `pyproject.toml`.
- [ ] **Step 4: Run to verify they pass**; `scripts/ci.sh`.
- [ ] **Step 5: Commit** `"herdr-fleet init: scaffold an operator repo"`.

### Task 3: Public-repo hygiene scan

**Files:**
- Create: `scripts/scan-public.sh`, `tests/test_scan_public.py`, `docs/public-hygiene.md`
- Modify: `.github/workflows/ci.yml`, `scripts/ci.sh`

**Interfaces:**
- Produces: `scripts/scan-public.sh [--denylist FILE]` fails (exit 1, file and line, never the matching secret text in full) on: private IPv4 and tailnet (100.64.0.0/10) addresses, `*.ts.net` names, `*.lan` names, email addresses outside an allowlist, tokens recognised by gitleaks, and any literal from an operator-supplied denylist file (the operator's host names, users). The same script is copied unchanged into `[eng]` and the other public repos by Task 20.

- [ ] **Step 1: Failing tests:** a planted `10.0.0.5`, `host.tail1234.ts.net`, `bender.lan` and a fake token each fail; a clean tree passes; the denylist literal fails; the output never prints the full token.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** with grep-based patterns plus `gitleaks` when present (pinned in `fleet.lock` already).
- [ ] **Step 4: Run to verify they pass**; add to CI.
- [ ] **Step 5: Commit** `"Scan public repos for operator-specific values"`.

### Task 4: New role names, order and placeholders

**Files:**
- Modify: `herdr_fleet/roles.py`, `herdr_fleet/schemas/fleet-config.schema.json`, `herdr_fleet/inventory.py` (report unlisted roles `not-implemented`), `docs/specs/0001-declarative-fleet-provisioning.md` (role table), `tests/test_plan.py`, `tests/test_config_schema.py`

**Interfaces:**
- Produces: roles added to the enum and `ROLE_ORDER` in this order after the existing ones they depend on: `tailscale` and `container-runtime` (after `baseline`, in that order), `powerpack` (after `herdr`), `host-agent` (after `herdr-engineering`), `control-plane`, `fabric-gateway` (replaces use of the spec's `gateway` for the control host; `gateway` stays accepted as an alias that plans to `control-plane` plus `fabric-gateway`), `jobs`, `memory`, `backup`, `campus-network`, `bench-agent`. Reserved for project 2, accepted but always reported `reserved`: `enroll-node`, `git-mirror`, `storage`, `agent-image`, `slurm-fabric`.

- [ ] **Step 1: Failing tests:** plan orders a host with every new role exactly as above; `gateway` expands as stated; reserved roles plan as `reserved` and never `apply`; an unknown role is still rejected.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the lists and the reserved handling.
- [ ] **Step 4: Run to verify they pass**; `scripts/ci.sh`.
- [ ] **Step 5: Commit** `"Role names for the base project and reserved names for the Slurm project"`.

## Slice 2: level 1 roles `[fleet]` unless marked

Every role task below follows `docs/adding-a-role.md` steps 1 to 10 in this order: pins (schema + example), role name (already added), cross-checks in `config.py`, failing tests (schema rejection, plan order, fingerprint `compare`), Ansible content with `defaults/main.yml` for non-pins, `tasks/fingerprint.yml`, `IMPLEMENTED_ROLES`, check-mode gating, container scenarios (fresh, converged, idempotent, drift, failure). A task is done only when all five scenarios pass in `tests/containers/` and `scripts/ci.sh` is green. The steps below name only what is specific to each role.

### Task 5: `baseline` and `container-runtime`

**Files:** `herdr_fleet/ansible/roles/baseline/**`, `herdr_fleet/ansible/roles/container-runtime/**`, `herdr_fleet/fingerprint.py`, schemas, tests, container scenarios.

**Interfaces:**
- `baseline` provides OS packages (git, curl, jq, vim, mc, btop; Homebrew equivalents on macOS), the state and config directories (`~/.local/share/herdr-engineering`, `~/.config/herdr-engineering`), and a Tailscale presence check that reports `ok`, `not-installed` or `not-joined` and never installs or logs in (login needs a key: the `tailscale` role of Task 5b). Pins: package names per OS (no versions; reported only).
- `container-runtime` provides Docker Engine on Debian and Ubuntu from the official repository with a pinned major version, the user in the docker group, and `docker compose`; it does nothing on macOS and reports `skipped: macos`. Needs root; a host without root reports `blocked: needs-root`.

- [ ] **Steps:** per the recipe, plus a scenario "macOS host plans no container-runtime step" and a scenario "no-root host reports blocked, other roles still run".
- [ ] **Commit** `"baseline and container-runtime roles"`.

### Task 5b: `tailscale` role

**Interfaces:** installs Tailscale (official apt repository on Debian and Ubuntu; the Homebrew formula on macOS), starts the daemon, and joins the tailnet only when the node is not already logged in, using an auth key read from the operator's secrets path (reusable tagged key by default, ephemeral when `tailscale.ephemeral: true`, tags from `tailscale.tags`); an already-joined host is left alone and never logged out; a missing key on a not-joined host reports `blocked: needs-auth-key` and does not fail the other roles. No key ever appears in output, extra-vars logs or the fingerprint (reported as present or absent only). A host without root reports `blocked: needs-root` unless `tailscale.userspace: true` (no-root mode, documented as limited). Fingerprint is the backend state, the tailnet name presence and the tags. The `enroll-node` role of project 2 reuses this role.

- [ ] **Steps:** per the recipe, with failing tests first: a joined host is a no-op and `--check` reports no change, a not-joined host without a key is `blocked`, the key never appears in captured output (planted key string searched in all logs), the schema rejects an auth key written into `fleet-config.yaml` (it must be a path); container scenarios use a stub `tailscale` binary that records its arguments.
- [ ] **Commit** `"tailscale role: install and join with a vaulted key"`.

### Task 6: `herdr` role

**Interfaces:** installs the pinned herdr binary (`herdr` pin: version plus per-platform sha256, existing schema) to `~/.local/bin`, verifying the checksum before use; fingerprint is `herdr --version` in a clean login shell; refuses a version outside the compatibility pin recorded in `fleet.lock` (`herdr.compat`: min and max, optional) with a message naming both versions.

- [ ] **Steps:** per the recipe; scenario "checksum mismatch fails and leaves the old binary", "herdr running as another user is reported, not touched".
- [ ] **Commit** `"herdr role"`.

### Task 7: `herdr-engineering` role

**Interfaces:** creates a release venv under `~/.local/share/herdr-engineering/releases/<commit12>/venv` from `repo_urls["herdr-engineering"]` at `repo_commits["herdr-engineering"]` on Python 3.12 or newer (uv, `python3.12`, or a Homebrew path; real version check; clear refusal otherwise), switches a `current` symlink, installs the `herdr-eng` shim to `~/.local/bin`; fingerprint is the commit and `herdr-eng --version`; rollback is switching the symlink back. The build gate and fabric agent unit files ship from this release (used by Task 12).

- [ ] **Steps:** per the recipe; scenarios: fork URL used (a local bare repo as the fork), re-run of the same commit never rebuilds the live venv, a failed build leaves `current` untouched.
- [ ] **Commit** `"herdr-engineering role"`.

### Task 8: `powerpack` role

**Interfaces:** installs `herdr-powerpack` at its pinned commit with `herdr plugin install <url> --ref <commit>`, writes `powerpack.enable.list` from the host's role config (which capabilities), runs `reconcile` and `doctor --json`, and fingerprints the doctor result. Roamgate's bind stays loopback or tailnet and never `0.0.0.0` (the Powerpack already enforces it; the role asserts it in the fingerprint). Fails the role only on a critical doctor finding.

- [ ] **Steps:** per the recipe; scenario "doctor reports a competing-state plugin: role fails with the plugin named".
- [ ] **Commit** `"powerpack role"`.

### Task 9: Publish pinned images from CI `[eng]`

**Files:** the CI configuration of `[eng]` (Woodpecker pipeline file or GitHub workflow, whichever the repo already uses for releases), `deploy/**/Dockerfile` as needed, `docs/releases.md`, tests that parse the pipeline.

**Interfaces:** on a version tag, CI builds the `herdr-engineering` image (the one used by `control-plane-web`, `-worker`, `llm-proxy`, collab, share, fabric gateway, `jobs`) for linux/amd64 and linux/arm64, pushes it to a public registry (GHCR) and writes the digest to the release notes and a machine-readable `release.json` (`{version, image: {repository, digest}, commit}`); no mutable tag is trusted by consumers. `herdr-fleet init` and a new `herdr-fleet pins update` read `release.json` to propose new pins (Task 20).

- [ ] **Step 1:** failing test that the pipeline definition contains the build, the push by digest, and the `release.json` step; and that the secret used for push is referenced by name only.
- [ ] **Step 2: Run to verify it fails. Step 3: Implement. Step 4: Run to verify it passes.**
- [ ] **Step 5: Commit** `"Release pipeline: publish the herdr-engineering image by digest"`. The first real release needs the owner's go-ahead (public registry and tag).

### Task 10: `control-plane` role

**Interfaces:** renders the compose project for the control host from the pinned image digests (`images` pins): `control-plane-web`, `control-plane-worker`, Postgres, rustfs, `llm-proxy`, collab public and edge, share public; takes the central fleet config (`fleet-config.yaml` of the control plane, not the provisioning file) from the operator's secrets path; mounts its directory read-only; runs `herdr-eng fleet config render-compose` from the pinned `herdr-engineering` commit to produce the compose variables in a 0600 file removed afterwards; brings the project up; fingerprint is the running image digests and `/api/v1/health`; `state:` lists the Postgres and rustfs volumes (used by Task 15). Adopting existing volumes is by name and never recreated; first step discovers the exact compose files and variables from `deploy/control-plane/` in `[eng]` and records them in the report.

- [ ] **Steps:** per the recipe; scenarios: fresh, converged (no recreate), rollback by previous digests, a failing health check rolls back automatically and leaves the old containers running.
- [ ] **Commit** `"control-plane role"`.

### Task 11: `fabric-gateway` role and the HTTPS default

**Interfaces:** coredns and the fabric gateway container (from the `fabric-stack` project of `[eng]`), certificates by Tailscale-issued HTTPS (`tailscale serve`, MagicDNS names) by default; AWS ACME renewal as an optional setting (`services.gateway.acme: aws`) that needs its credentials path in the operator secrets and is validated by `config.py`; fingerprint is the served certificate expiry and the gateway health endpoint.

- [ ] **Steps:** per the recipe; scenarios: default needs no cloud account, AWS setting without its credentials is rejected naming file and key.
- [ ] **Commit** `"fabric-gateway role and Tailscale HTTPS default"`.

### Task 12: `host-agent` role

**Interfaces:** from the pinned `herdr-engineering` release (Task 7): installs the herdr plugins (`herdr.cp-feed`, `herdr.pi-recovery`) via `herdr plugin install` at pinned commits, the service-metadata feed, the fabric agent, the mission feed and the build cleanup as user services (systemd user units on Linux with `loginctl enable-linger` check; launchd agents on macOS with the account name substituted and `bootstrap`/`kickstart`, never legacy `load`), obtains the host token with the existing `herdr-eng fleet join` (needs the control plane reachable; the role orders after `control-plane` on the same host and treats an unreachable control plane as `blocked: control-plane-unreachable`, not as failure), writes `host.json` mode 0600; the host-native timers (fleet update, capacity watchdog, fabric certificates) are units of this role; fingerprint is unit states plus a successful report visible on the control plane. Supersedes `deploy-fleet-agents.sh` for these services (retired in Task 21).

- [ ] **Steps:** per the recipe; scenarios on Debian-family container and a macOS fixture of the rendered plist; "re-run with the same release does not restart a healthy unit"; "unit failing for 20 s rolls back to the previous release".
- [ ] **Commit** `"host-agent role"`.

### Task 13: `jobs` role

**Files:** role, plus in `[eng]` a `jobs` subcommand if the scheduler needs one.

**Interfaces:** a scheduler container (supercronic or equivalent chosen in the first step with a one-paragraph reason; pinned digest) from the same `herdr-engineering` image or its own pinned image, running a generated `jobs.yaml` (`name`, `schedule`, `command`, `timeout`, `overlap: skip`) for the former control-host timers: collab sync, collab certificate renewal, CI report relay, pipelines sync, gateway monitor, fabric certificates, GitHub automerge, AutoSpec fleet sync, and the backups of Task 15. The first step inventories every existing timer on the control host (from the owner's bender, read-only via the repo's unit files in `[eng]`, not by touching the host) and maps each to a job or to "stays native".

- [ ] **Steps:** per the recipe; scenarios: a job runs on schedule (accelerated clock), overlapping runs are skipped, a job exceeding its timeout is killed and reported, logs reach the container log.
- [ ] **Commit** `"jobs role: timers as containers"`.

### Task 14: `memory` role (optional)

**Interfaces:** Hindsight and the memory API with their databases as containers pinned by digest, only when the host lists the role; its source repo is private today, so the role is `optional` in the schema documentation and its images come from a registry the operator names (`images.memory-*` pins); state lists both databases; backups are jobs (Task 13) writing to the Task 15 target.

- [ ] **Steps:** per the recipe; scenario "role absent changes nothing"; "images from a private registry need `registry_auth` in operator secrets, otherwise a clear error".
- [ ] **Commit** `"memory role (optional)"`.

## Slice 3: operations

### Task 15: Backup and restore

**Files:** `herdr_fleet/backup.py`, `herdr_fleet/ansible/roles/backup/**`, cli (`backup`, `restore`), tests, a container scenario with a throwaway control plane.

**Interfaces:** `backup` role reads each role's `state:` list and installs a backup job (Task 13) per state: Postgres `pg_dump` custom format, rustfs bucket sync, Hindsight and memory dumps, collab data; target is operator config (`backup.target`: `path`, `s3`, or `host:path` over ssh) and must not be on the same host (validated: the target host differs from every source host, or an `s3` URL); `herdr-fleet backup now [--host H]` runs it; `herdr-fleet restore --from TARGET --host H` rebuilds a clean control plane from the lock and the backup; the report states what was restored and checks `/api/v1/health` plus a row count per database against the manifest written by the backup. Backups are encrypted with a key from the operator's keystore.

- [ ] **Steps:** failing tests for target validation (same-host target rejected), manifest format, encryption round trip; container scenario: start a control plane, write data, back up, destroy it, restore on a fresh container, compare manifest and health.
- [ ] **Commit** `"backup and restore"`.

### Task 16: `controller` role and unattended apply (opt-in)

**Interfaces:** the controller host keeps the operator repo checkout read-only for an update runner; `herdr-fleet apply` can be started by a systemd timer or launchd job only when `controller.unattended: true` is set (default false); the vault password file is read from the controller's keystore path (mode 0600, validated) or from the environment in CI, never from the repo; staggered, session-aware (busy hosts deferred up to `max_defer_days`, from the control plane's busy data, same behaviour the fleet update has today); every run posts a result event to the control plane; fingerprint is timer state and last result.

- [ ] **Steps:** per the recipe; scenarios: default off installs no timer; a missing or world-readable vault password file refuses to start with the path named; busy host deferred; deferred longer than the limit runs only with `--force`.
- [ ] **Commit** `"controller role: unattended apply is opt-in"`.

### Task 17: `herdr-fleet adopt` (existing hosts)

**Files:** `herdr_fleet/adopt.py`, cli, tests, container scenario with a hand-made host fixture.

**Interfaces:** `herdr-fleet adopt <host> --config OPERATOR_DIR` inspects an existing host read-only over the normal connection (no writes, no restarts): which compose projects and containers (names, images, volumes), user services and timers, herdr plugins and their sources, and the config directories (names only, never contents); writes a draft `fleet-config.yaml` fragment and a draft lock fragment with the discovered image digests and commits, plus an `adoption-report.md` that classifies everything as `managed by a role`, `adoptable with data in place`, `not part of the fleet` (the owner's unrelated stacks are classified here by an operator-supplied ignore list, default none), or `unknown`; a staged `herdr-fleet apply --adopt` converges one role at a time in the order of the report with a backup first (Task 15), never recreates a named volume, and on a failed health check restores that service's previous container and continues with the report's next safe step.

- [ ] **Steps:** failing tests on a fixture host (a container running the hand-made layout): adopt writes nothing on the host (file listing before and after equal), the report classifies each fixture item, a planted unrelated compose project lands in `not part of the fleet`, `apply --adopt` keeps the Postgres volume and its data, a forced failure of one service leaves the others running and is reversible.
- [ ] **Commit** `"adopt: take over existing hosts without downtime beyond restarts"`.

## Slice 4: capture, retire, document, adopt for real

### Task 18: Per-run resource capture `[eng]`

**Files:** in `[eng]`: migration for a `resource_runs` table, an ingest route, the host-agent collector, tests.

**Interfaces:** `POST /api/v1/ingest` accepts an event kind `resource_run` with `{project, agent_kind, session_id, host, started_at, ended_at, peak_memory_bytes, cpu_seconds, disk_growth_bytes, exit_reason, hit_limit}`; a collector in the host agent samples cgroup counters (Linux) or process stats (macOS) per agent session and posts one event per ended session; the control plane stores it keyed by project and agent kind and exposes `GET /api/v1/resource-profiles?project=` with count, median, p95 and max per metric (read only; no learning in this project). First step reads the existing host metrics module and capacity watchdog and records what is reused.

- [ ] **Steps:** failing tests for the schema and route (bad values rejected), the percentile endpoint on a seeded table, the collector on a recorded fixture; Postgres migration up and down.
- [ ] **Commit** `"Capture per-run resource usage per project"`.

### Task 19: Retire `herdr-web` `[eng]`

**Files:** `deploy/control-plane/compose.yaml`, `deploy/fabric-stack/README.md`, `herdr_engineering/web.py`, `cli.py` (`web` command), `docs/specs/0060-web-mobile-control-surface.md`, tests that reference them, the Agent Mine playbook and cache only after the check below.

- [ ] **Step 1:** check what still reads Agent Mine (`session_history.py`, `agent_mine.py`, `ansible/playbooks/agent-mine.yml`) and what else imports `web`; list in the report; keep anything with another reader.
- [ ] **Step 2:** failing test that the compose project has no `herdr-web` service and the CLI has no `web` command; run to see it fail.
- [ ] **Step 3:** remove the service, command, module, spec and docs references; mark spec 0060 retired with the date.
- [ ] **Step 4:** run the suites; **Step 5: Commit** `"Retire herdr-web (nobody uses it)"`. Removing the container from bender happens with the `control-plane` role adoption (Task 21), not by hand.

### Task 20: Documentation, pin updates and the clean-machine scenario

**Files:** `[fleet]` `docs/quickstart.md`, `docs/adopt-your-host.md`, `docs/troubleshooting.md`, `docs/forking.md`, `herdr_fleet/pins.py` (`pins update`), `tests/containers/test_from_nothing.py`; `[eng]` and other public repos: copy `scripts/scan-public.sh` and add it to their CI.

**Interfaces:** `herdr-fleet pins update [--from release.json]` shows the commits that changed in each of our repos since the operator's pins (compare API) and writes proposed pins to a file, never in place; the quickstart takes a stranger from a blank Debian container to a running level-1 fleet in under ten commands; the scenario `test_from_nothing` runs it end to end with `init`, `apply`, health, second `apply` no-op, adding a second (container) host with a few lines, upgrade by one pin, and rollback by the old lock: these are definition-of-done checks 1 to 4.

- [ ] **Steps:** failing scenario first (it fails at the first missing piece); then docs written from the passing scenario so every command in the quickstart is copied from the test; a link checker over the docs; scan in each repo's CI.
- [ ] **Commit** `"Quickstart, forking guide, pin updates and the from-nothing scenario"`.

### Task 21: The owner's fleet repo, real adoption, and retiring the old paths

**Files:** a new private operator repo (name chosen by the owner) created with `herdr-fleet init`; deletions in `[eng]` (`ansible/`, `deploy/deploy-fleet-agents.sh`, the fleet update and self-update scripts, the fleet auto-update timer, `fleet-sync.yml`, `site.yml`) and a `herdr-eng fleet` shim that delegates to `herdr-fleet`.

**This task changes the owner's real hosts. Each host needs the owner's explicit go-ahead, one host at a time, in this order: beast, the Mac, fry, bender (control host last).**

- [ ] **Step 1:** create the operator repo (private), run `adopt` read-only against each host, commit the draft config and lock, and review the adoption reports with the owner.
- [ ] **Step 2:** for the first host: backup, `apply --adopt` (dry run first), health, second `apply` no-op, `plan --check` no drift; repeat per host. Bender adopts the control plane roles last, with the `herdr-web` container removed by the role and the host-native timers and the `jobs` container in place.
- [ ] **Step 3:** run the restore test of Task 15 against a real backup of the owner's control plane on a throwaway machine (definition of done 7).
- [ ] **Step 4:** when all hosts converge with no unexpected drift (definition of done 6), delete the old paths in `[eng]` in one PR, verified green in CI, and replace `herdr-eng fleet` with the shim.
- [ ] **Step 5:** update the memory note and the backlog with what changed.

## Notes for the executor

- The agent runtime, the single agent image, login sync, `storage`, `git-mirror`, `enroll-node` and the Slurm fabric are project 2 and are only reserved here (Task 4).
- Two tasks need decisions that arrive with evidence from their first step and are recorded in their reports, not guessed here: the exact compose inputs of the control plane (Task 10) and the scheduler inside the `jobs` container (Task 13).
