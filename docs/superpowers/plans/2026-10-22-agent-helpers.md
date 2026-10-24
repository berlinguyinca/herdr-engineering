# Agent helpers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agents in a collaboration room can start sandboxed helpers on their own (no per-spawn approval), under room profiles, limits and an operator-approved network allowlist.

**Architecture:** The collab service (`collab.py`) owns helper requests, limits, the room profile (names and policy only, never secret values) and host-access requests. A new MCP tool family (`helper_*`) lets an agent ask. The host runner (`collab-spawn.py`) claims requests, mints the helper's own agent credential, places it in a pane under its parent, and runs the harness inside bubblewrap with **no network** except a runner-controlled egress proxy (`collab-egress.py`) that enforces the room allowlist and injects API keys for bound services. Model choice is made server-side by a pure picker (`collab_models.py`).

**Tech Stack:** Python 3.12 (stdlib only for the proxy), pytest, bubblewrap, vanilla ES modules (`packages/collab-*.js`, `node --test`), existing herdr CLI fake in `tests/control_plane/test_collab_spawn.py`.

**Spec:** `docs/superpowers/specs/2026-10-22-agent-helpers-design.md`

## Global Constraints
- Flag-gated: nothing changes unless `control_plane.collab.helpers.enabled: true` in the central config; existing endpoints and tests keep passing.
- No secret values in GitHub, the event log, task text, logs or API responses. Secret values live only in `~/.config/herdr-engineering/collab-secrets/<room_id>.json` (mode 0600) on the runner host.
- An agent never supplies a command, path, env or secret: `helper_spawn` accepts only `room_id, role, task, family, model, wait`.
- Roles: `reviewer`, `debugger`, `worker`, `vision`, `successor`.
- Defaults: 3 live helpers per agent; depth 2; 20 spawns per person per hour; helper lifetime 7200 s; host approval TTL 86400 s; denial remembered 3600 s; successor chain cap 5.
- A helper's grants are the intersection of its parent's grants and the room's, minus nothing it did not already have; it never gains `grant_agent`.
- Remote task text and helper results are untrusted (`_text(..., untrusted=True)` in `collab_mcp.py`).
- The spend ceiling is optional and off unless the room profile sets it.
- Dashboard stays private; the public site gains only the existing MCP path.
- Commit trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`; deploy only via `main`'s `deploy/control-plane/deploy.sh --ref origin/main`.

## Review Focus
- A prompt-injected helper tries to reach an unlisted host, or exfiltrate a bound API key in its output: blocked / key never present (Task 4, Task 7).
- A coworker's agent without `spawn_helpers` calls `helper_spawn`, or a helper tries to spawn past depth 2 (Task 1, Task 5).
- Parent revoked or panic switch pressed with helpers running: whole subtree stops, no orphan keeps its credential (Task 5, Task 6).
- Runner restarts mid-helper or claims twice: a helper starts at most once, an abandoned one is expired (Task 6).
- Task text with shell metacharacters, huge text, or non-UTF8 bytes: passed on stdin only, size-limited (Task 5, Task 6).

---

### Task 1: `spawn_helpers` permission
**Files:** Modify `herdr_engineering/control_plane/collab.py` (lines ~40-45 `ROLE_ACTIONS`, ~61 `AGENT_ACTIONS`), `packages/collab-view.js` (line 15 `PERMS`, the `permBoxes` lists), `tests/control_plane/test_collab_tasks.py`.
**Interfaces:** Produces: action string `"spawn_helpers"` allowed for roles `owner`, `contributor`; valid in agent grants via `_actions()`. Consumes: nothing.
- [ ] Failing test `test_spawn_helpers_is_an_owner_approved_agent_action`: owner grants an agent `["read","post","spawn_helpers"]` → grant stored with it; viewer role cannot receive it (action silently dropped by the `ROLE_ACTIONS ∩ AGENT_ACTIONS` intersection); a user token cannot hold it; an unknown action still 400s.
- [ ] Add the action to `ROLE_ACTIONS` (owner, contributor) and `AGENT_ACTIONS`; add `['spawn_helpers', 'Start helper agents']` to `PERMS`.
- [ ] Run `python3 -m pytest tests/control_plane/test_collab_tasks.py -q` → PASS; `npm run test:client` → PASS. Commit.

### Task 2: Room profile (policy, no secret values)
**Files:** Modify `collab.py`; Create `tests/control_plane/test_collab_helpers_profile.py`.
**Interfaces:** Produces on `CollabService`:
`set_room_profile(pr, room_id, body) -> dict` (owner only, action `grant_agent`), `get_room_profile(pr, room_id) -> dict`. Profile fields: `allowed_families: list[str]` (⊆ `SPAWN_FAMILIES`), `allowed_models: list[str] | None`, `tools_allowed: bool`, `permanent_hosts: list[str]` (`host:port`, validated `[a-z0-9.-]+:\d{1,5}`, ≤50), `services: list[{name, upstream_url, header, secret_name}]` (https URLs only, `secret_name` references the host file, ≤20), `ssh_secret_names: list[str]`, `spend_tokens_per_day: int | None`, `limits: {live_per_agent, depth, per_hour}` (may only lower the defaults). Event `room_profile_set` (no secret values exist in it by construction). Internal `_profile(room_id) -> dict` returning defaults when unset.
- [ ] Failing tests: owner sets/gets; non-owner 404; `http://` upstream rejected; limits above defaults rejected; hosts malformed rejected; profile survives `hydrate` replay; event payloads contain only names.
- [ ] Implement with the existing `_commit`/`_apply` pattern; add routes `GET|PUT rooms/{id}/profile` in `dispatch` next to the other room routes.
- [ ] Tests PASS; commit.

### Task 3: Host-access requests with expiring approvals
**Files:** Modify `collab.py`; Create `tests/control_plane/test_collab_egress_requests.py`.
**Interfaces:** Produces: `request_egress(runner_pr, room_id, agent_id, host, port) -> dict` (called by the runner as the owner; dedups open requests; a denied host returns `{"state":"denied"}` for 3600 s), `list_egress(pr, room_id=None) -> dict` (operator sees all, owner sees own rooms), `decide_egress(operator_pr, request_id, decision, ttl_seconds=86400) -> dict`, `promote_egress(operator_pr, room_id, host_port)`, `remove_egress(operator_pr, room_id, host_port)`, `egress_allowlist(room_id) -> list[str]` (permanent ∪ unexpired approvals; used by the runner via `GET fleet/egress/{room}`). Only the operator may decide/promote/remove. Events `egress_requested|decided|promoted|removed`. Model endpoints of each family are returned as a constant `FAMILY_ENDPOINTS: dict[str, list[str]]` merged into every allowlist.
- [ ] Failing tests: unlisted host → pending; operator approves → allowlist contains it until `ttl`, then not (`_now` injectable); denial blocks re-request for 3600 s; promote makes it permanent; a room owner who is not the operator cannot approve (404); allowlist is per room (another room unaffected); request flood capped (60 open per room).
- [ ] Implement; add operator-only routes under `fleet/egress*`; commit.

### Task 4: Egress proxy and sandbox networking
**Files:** Create `deploy/control-plane/collab-egress.py`, `packages/collab-scripts` copy is NOT needed; Modify `deploy/control-plane/collab-sandbox.sh`, `deploy/control-plane/collab-sync.sh` (install list); Create `tests/control_plane/test_collab_egress.py`.
**Interfaces:** Produces `collab-egress.py serve --sock PATH --allow-file PATH --secrets-file PATH --log PATH` (host side; HTTP `CONNECT host:port` tunnel; allowlist file re-read per connection; blocked → `403` and a line appended to `--log` as `{"host","port"}` for the runner to turn into `request_egress`) and `collab-egress.py relay --sock PATH --listen 127.0.0.1:PORT` (runs inside the sandbox). Service routes: plain HTTP `GET|POST /svc/<name>/<path>` on the same socket; the proxy forwards to the bound `upstream_url` over TLS adding the configured header from the secrets file; the helper never receives the value. `collab-sandbox.sh` gains: when `HERDR_EGRESS_SOCK` is set, add `--unshare-net`, bind the socket read-only, start the relay, export `HTTPS_PROXY=http://127.0.0.1:PORT` and `SVC_<NAME>_URL=http://127.0.0.1:PORT/svc/<name>`; without the variable behavior is unchanged (existing workers).
- [ ] Failing tests (run the proxy as a subprocess with a local TCP echo server standing in for an upstream): allowed host:port tunnels; unlisted host gets 403 and a log line; port mismatch refused; hostnames resolving to loopback/link-local/private ranges refused unless explicitly allowlisted by IP; `/svc/x` forwards with the header and the response never echoes the secret; relay inside `bwrap --unshare-net` (skip if bwrap missing) cannot reach a LAN address directly but can reach the allowed echo through the relay.
- [ ] Implement with `asyncio` streams; sandbox changes as above; `bash -n` on the script.
- [ ] Tests PASS; commit.

### Task 5: Helper requests, limits, subtree revoke, panic switch
**Files:** Modify `collab.py`; Create `tests/control_plane/test_collab_helpers.py`.
**Interfaces:** Produces on `CollabService`: `request_helper(agent_pr, body) -> dict` (`body`: `room_id, role, task, family?, model?`; requires `spawn_helpers` in the agent's room grant; enforces live/depth/hour limits, `helpers_paused`, family in `allowed_families`, `task` ≤ 8000 chars; sets `parent` from the caller's agent id server-side; for `role=="successor"` replaces the parent in the tree and counts chain length ≤ 5), `helper_status(pr, helper_id) -> dict` (caller must be the requester or its owner), `helper_result(pr, helper_id)`, `cancel_helper(pr, helper_id)`, `list_helpers_for_runner(owner_pr) -> dict`, `claim_helper(owner_pr, helper_id)`, `finish_helper(owner_pr, helper_id, body)`, `set_helpers_paused(operator_pr, paused: bool)`. Revoking an agent (`revoke_agent`) also cancels its helper subtree and revokes the credentials of helper agents under it. Helper record statuses: `requested, claimed, running, done, failed, cancelled, expired`; lifetime from the profile (≤ 7200 s). Model/tier: calls `collab_models.pick(...)` (Task 6) and stores `model` and `model_reason`. Failure over a limit is a `CollabError(429, "<which limit>")`, never a queue.
- [ ] Failing tests (one per limit and per refusal): no `spawn_helpers` → 403; 4th live helper → 429; depth-3 → 429; 21st in an hour → 429; paused → 503 with message; helper cannot name another room or raise its grants; parent claim in `body` ignored; revoke parent cancels all descendants and kills their agent credentials (`authenticate` fails); successor replaces parent, chain of 6 refused; `task` with `\0`, control chars, or > limit rejected; the audit-log line for a spawn holds ids, role and model only, never the task text.
- [ ] Implement with events `helper_requested|claimed|finished|cancelled|expired`; operator routes `POST fleet/helpers/pause`; runner routes `GET fleet/helpers`, `POST fleet/helpers/{id}/claim|finish`; `Condition.notify_all()` on change so `helper_wait` wakes.
- [ ] Tests PASS; commit.

### Task 6: Model picker (rules first)
**Files:** Create `herdr_engineering/control_plane/collab_models.py`, `tests/control_plane/test_collab_models.py`; Modify `collab.py` (catalogue store).
**Interfaces:** Produces `pick(role: str, task: str, family_hint: str | None, model_hint: str | None, catalogue: list[dict], allowed: list[str] | None, author_model: str | None) -> Pick` with `Pick(model: str | None, tier: str, reason: str)`; tiers `trivial|standard|hard|vision`. Rules: `vision` role or task mentions image/screenshot → needs a catalogue entry advertising vision; `reviewer|debugger` never returns `author_model`; trivial = task < 400 chars and no review/debug role and no words in {"refactor","race","deadlock","security"}; choose the cheapest allowed model whose capability ≥ tier (catalogue fields as served by the gateway `/v1/models`); `model_hint` honored only if allowed and capable, else ignored with reason; no eligible model → `Pick(None, tier, "blocked: ...")` (never a default name). Service stores the latest catalogue via operator `POST fleet/catalogue` (runner posts it every 10 min).
- [ ] Failing tests: trivial task → cheapest; "debug this deadlock" → strongest allowed; reviewer ≠ author; vision requires vision-capable; hint outside `allowed` ignored; empty catalogue → blocked; deterministic for equal scores (sort by id).
- [ ] Implement; commit.

### Task 7: Runner executes helpers
**Files:** Modify `deploy/control-plane/collab-spawn.py` (new `Runner.helpers_cycle`, `Runner.run_helper`; reuse `herd_pane`, `link_target`, `place`), Create `deploy/control-plane/collab-secrets.py` (CLI: `set ROOM NAME` reading the value from stdin, `list`, `rm`, files 0600), Modify `tests/control_plane/test_collab_spawn.py` (reuse `FAKE_HERDR`), Create `tests/control_plane/test_collab_helper_runner.py`.
**Interfaces:** Consumes Task 5 routes, Task 3 `egress_allowlist`, Task 4 proxy/sandbox. Produces: per cycle `helpers_cycle()` lists, claims (exactly once; a second claim 409 is skipped), mints the helper agent via the existing `agents/connect` with `parent_agent_id` = the requesting agent, grants it the intersection, places a pane under the parent's tab (`herd_pane`), writes the allow-file from `GET fleet/egress/{room}` and starts one `collab-egress.py serve` per helper, injects secrets by file for `ssh_secret_names` only, mounts the parent workdir read-only for `reviewer|debugger`, runs the harness command with the task on stdin under `timeout` = lifetime, posts `finish` with the (size-limited, 20 KB) output, then tears down proxy, workdir and credential. Blocked-host log lines become `request_egress` calls; while pending the helper's connection gets 403 and the helper retries by itself (it is told in its role prompt).
- [ ] Failing tests with fake herdr and a fake harness script: claim-once; pane lands in the parent's tab; harness sees task on stdin only and `HERDR_SANDBOX=1`; env has no secret values; ssh key file present only when listed; timeout kills and marks `failed`; runner restart (new `Runner`) does not restart a `claimed` helper and expires it after `SPAWN_STALE`; panic switch or revoke stops a running helper within one cycle.
- [ ] Implement; install both scripts via `collab-sync.sh install`; commit.

### Task 8: MCP tools
**Files:** Modify `herdr_engineering/control_plane/collab_mcp.py` (`TOOLS`, `_call`), `tests/control_plane/test_collab_helpers.py`.
**Interfaces:** Produces tools `helper_spawn(room_id, role, task, family?, model?, wait?: bool)` → `{helper_id}` (or the final result when `wait` and it completes within 20 s), `helper_wait(helper_id, seconds ≤ 20)`, `helper_result(helper_id)`, `helper_cancel(helper_id)`. Unknown arguments → `CollabError(400, "unknown argument")` exactly like `task_create`. Results returned with `untrusted=True`.
- [ ] Failing tests: `tools/list` includes the four; passing `command`, `cwd`, `env`, `parent_agent_id` or `secrets` → 400; only the requester can read its helper's result; long-poll wakes within 1 s of `finish_helper`.
- [ ] Implement; commit.

### Task 9: Dashboard
**Files:** Modify `packages/collab-view.js`, `packages/collab.css`, `packages/herdr-web-client/collab.js` (API wrappers `roomProfile`, `setRoomProfile`, `egressRequests`, `decideEgress`, `promoteEgress`, `removeEgress`, `helpers`, `pauseHelpers`); Create `test/collab-helpers-ui.test.js`.
**Interfaces:** Consumes Tasks 2, 3, 5 routes. Produces: room profile editor (owner), "Host access requests" list with Approve 1 day / Approve 1 week / Always / Deny (operator only), per-room external hosts table with Remove, helper tree under the parent in the flow detail, and a panic switch button.
- [ ] Failing JS tests with a fake API: buttons call the right wrappers; non-operator sees no approve buttons; panic switch shows its state; XSS strings in host/task are escaped.
- [ ] Implement; `npm run test:client` → PASS; commit.

### Task 10: Documentation and adversarial tests
**Files:** Modify `docs/operations/collaboration-network-guide.md`, `packages/collab-guide.md`; Create `tests/control_plane/test_collab_helpers_pentest.py`.
- [ ] Pentest cases, each failing before the guard exists: prompt-injection text in `task` cannot change role/limits; a helper calling `agents/connect`, `fleet/*` or user routes with its credential gets 403; secret names appear nowhere in `GET` responses beyond names; two helpers of different rooms cannot read each other's workdirs (separate dirs, no cross-bind).
- [ ] Document: flag, permission, profile, host approvals, secrets CLI, limits table, panic switch, residual risks (SSH keys are readable by the helper; allowlisted hosts are room-wide; external review still pending).
- [ ] Full run: `python3 -m pytest tests/control_plane -q` and `npm run test:client` PASS; ruff on touched files; commit.

### Task 11: Deploy and live verification
- [ ] PR, merge, deploy from a clean worktree of origin/main with `deploy.sh --ref origin/main`; enable the flag in `~/.config/herdr-engineering/fleet-config.yaml` only after the deploy is healthy.
- [ ] Live: an approved test agent with `spawn_helpers` spawns a reviewer; it sees the pane under its parent; a request to an unlisted host appears on the dashboard, approval lets the next attempt through, expiry blocks it again; panic switch stops it. Clean up test agents, panes and secrets files.

### Task 12 (optional, only if the operator sets a ceiling): spend ceiling
- [ ] First find how the gateway exposes per-key usage (read-only look); if there is no per-room attribution, report back before building. Otherwise count tokens per room per day from the gateway usage source, refuse spawns over the ceiling with 429, and test with a fake usage source. Skipped entirely when `spend_tokens_per_day` is `None`.
