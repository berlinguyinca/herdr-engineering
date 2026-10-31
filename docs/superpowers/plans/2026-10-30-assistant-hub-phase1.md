# Assistant Hub, Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A tailnet-only MCP endpoint on the personal assistant that Claude Code installations enroll with, can ask and report to, and can read assistant state through; the assistant records everything and decides nothing yet (shadow).

**Architecture:** A new package `herdr_engineering/control_plane/assistant_hub/` holds a worker registry (digest-only credentials), a network guard, an MCP JSON-RPC handler and its own HTTP listener, separate from the public collab listener. Owner-only enrollment/revoke routes stay on the existing operator API and a CLI. Worker state is persisted in the existing `AssistantStore` snapshot. A stateless Claude Code hook relay forwards observations to the hub.

**Tech Stack:** Python (stdlib `http.server`, `ipaddress`, `secrets`, `hashlib`), pytest with real servers over real sockets (no mocks), existing `AssistantService`/`AssistantStore`.

**Spec:** `docs/superpowers/specs/2026-10-30-assistant-claude-mcp-and-jev-design.md` (sections 5, 13, 14, 15, 16). Phases 2-4 (verb grants, Jev judge, dynamic grants) and the Codex/OpenCode/Pi adapters are separate later plans.

## Global Constraints

- Branch `feat/assistant-jev-mcp` in worktree `/home/wohlgemuth/IdeaProjects/herdr-assistant-jev`; never commit to main; no merge without the owner's word; plain `git push` only.
- TDD; no mocks, stubs or fakes (a `now` clock injection is allowed, as in existing tests); tests use throwaway HOME/tmp dirs and never touch the real `~/.claude`, `~/.codex`, `~/.config/opencode`, `~/.pi`.
- Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Credential format `hcw_` + `secrets.token_urlsafe(32)`; enrollment code `hce_` + `secrets.token_urlsafe(24)`, single use, TTL default 900 s; only sha256 digests stored; shown once; uniform 401 on every auth failure.
- Authorization scheme `Worker <cred>`; device header `X-Herdr-Device`; MCP protocol version `"2025-03-26"`; path `POST /assistant/mcp`; default port 9483.
- Allowed peer networks by default: `127.0.0.0/8`, `::1/128`, `100.64.0.0/10`, `fd7a:115c:a1e0::/48`. The listener refuses to start on any other bind address. `X-Forwarded-For` is ignored. Requests carrying `Origin` are rejected. The hub is never mounted on the public collab listener or nginx edge.
- Phase 1 never answers a question or authorizes an action: `assistant_ask` returns `{"status":"no_decision","mode":"shadow"}` after recording the ask.
- Provenance of recorded events: `worker_reported` for anything arriving via the MCP `report`/`assistant_ask` tools; `harness_attested` only via the hook-event route. No Phase 1 evidence activates anything.
- Credentials, enrollment codes and `Authorization` values never appear in audit entries, logs, snapshots (beyond digests), CLI output after first display, or MCP output.
- Python lint/format per repo config before each commit; run `python3 -m pytest tests/control_plane/test_assistant_hub_*.py -q`.

## Review Focus

- A peer inside an allowed network spoofing `X-Forwarded-For`/`Host`, and IPv4-mapped IPv6 peers (`::ffff:100.64.1.2`): must classify by the real socket address.
- Enrollment code replay, expiry and a code redeemed with a different device id: second use, expired and mismatched device all get the same 401 as a bad credential.
- A revoked or expired worker credential mid-session, and a worker from tenant A reading tenant B's events or state: both refused.
- Oversized body, ambiguous framing (two `Content-Length`, `Transfer-Encoding`), non-JSON content type, `Origin` header: refused before any parsing of the body into state.
- A prompt-injected model calling `report` claiming `harness_attested` or "owner approved": stored as `worker_reported` regardless of payload. **Known limit, stated to the owner:** a model with shell access to the same user's files can read the hook credential and call the hook route; Phase 1 evidence activates nothing, and Phase 2/4 plans must add a hook-only secret or a stronger channel before attested evidence counts.

---

### Task 1: Hub configuration section

**Files:**
- Modify: `herdr_engineering/control_plane/assistant.py` (`assistant_config`, near the other section normalizers ~:130-248)
- Test: `tests/control_plane/test_assistant_hub_config.py`

**Interfaces:**
- Produces: `assistant_config(cfg)["hub"]` as `{"enabled": bool, "host": str, "port": int, "allowed_networks": list[str], "allowed_hosts": list[str], "enrollment_ttl_seconds": int, "worker_ttl_days": int, "tenant": str}`.

- [ ] **Step 1: Write failing tests** `test_hub_defaults_off`, `test_hub_defaults_values` (`host "127.0.0.1"`, `port 9483`, networks = the four defaults, `enrollment_ttl_seconds 900`, `worker_ttl_days 90`), `test_hub_rejects_unknown_key`, `test_hub_rejects_non_mapping`, `test_hub_rejects_bad_port` (0, 70000, "9483"), `test_hub_rejects_bad_network` (`"not-a-cidr"`), `test_hub_enabled_must_be_bool`, `test_hub_tenant_validated_with_existing_tenant_rule`.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_assistant_hub_config.py -q`. Expected: FAIL (`KeyError: 'hub'`).
- [ ] **Step 3: Implement** `_hub_config(raw: Any) -> dict[str, Any]` in `assistant.py` (use `ipaddress.ip_network(strict=True)` and the existing `_tenant_id`), call it from `assistant_config`. Unknown keys raise `ValueError`, like `apps`/`ci_watch`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(assistant): hub config section`.

### Task 2: Network guard

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/__init__.py`, `herdr_engineering/control_plane/assistant_hub/netguard.py`
- Test: `tests/control_plane/test_assistant_hub_netguard.py`

**Interfaces:**
- Produces:
  - `parse_networks(cidrs: list[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]`
  - `bind_allowed(host: str, networks) -> bool` (host is an IP literal; hostnames are resolved by the caller, never accepted here)
  - `peer_allowed(peer: str, networks) -> bool` (unwraps IPv4-mapped IPv6 before matching)
  - `host_header_allowed(header: str | None, allowed_hosts: list[str]) -> bool` (case-insensitive, port stripped, empty list means only the bind address and loopback names)

- [ ] **Step 1: Write failing tests**: `test_bind_loopback_and_tailnet_ok`, `test_bind_wildcard_refused` (`0.0.0.0`, `::`), `test_bind_public_ip_refused` (`8.8.8.8`), `test_peer_tailnet_v4_v6`, `test_peer_ipv4_mapped_unwrapped` (`::ffff:100.64.1.2` allowed, `::ffff:8.8.8.8` refused), `test_peer_outside_refused`, `test_host_header_allowlist_and_port_strip`, `test_host_header_missing_refused`.
- [ ] **Step 2: Run to fail.**
- [ ] **Step 3: Implement** the four functions with `ipaddress`; no DNS.
- [ ] **Step 4: Run to pass.**
- [ ] **Step 5: Commit** `feat(assistant-hub): tailnet network guard`.

### Task 3: Worker registry and persistence

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/workers.py`
- Modify: `herdr_engineering/control_plane/assistant.py` (`AssistantStore.__init__`, `snapshot`, `restore`: add `workers` and `enrollments` dicts the same way as `proposals`)
- Test: `tests/control_plane/test_assistant_hub_workers.py`

**Interfaces:**
- Produces (in `workers.py`):
  - `@dataclass Worker`: `worker_id, tenant_id, user_id, name, harness, device_id, secret_digest, status, created_at, expires_at, last_seen_at, scopes`; `.as_dict()` never contains `secret_digest`.
  - `@dataclass Enrollment`: `enrollment_id, tenant_id, user_id, name, harness, code_digest, expires_at, redeemed_at`.
  - `class WorkerAuthError(Exception)`.
  - `class WorkerRegistry(store: AssistantStore, *, enrollment_ttl: timedelta, worker_ttl: timedelta, now: Callable[[], datetime] = ...)` with `create_enrollment(*, tenant_id, user_id, name, harness) -> tuple[Enrollment, str]`, `redeem(code: str, *, device_id: str) -> tuple[Worker, str]`, `authenticate(authorization: str, device: str | None) -> Worker`, `revoke(worker_id: str, *, tenant_id: str, user_id: str) -> Worker`, `list_workers(*, tenant_id: str, user_id: str) -> list[Worker]`.
  - `harness` validated against `assistant_dispatch.HARNESSES` (`pi`, `claude`, `codex`, `opencode`).
- Default scopes: `["ask", "report", "read"]`.

- [ ] **Step 1: Write failing tests**: `test_enrollment_code_is_single_use`, `test_enrollment_expires` (inject `now`), `test_redeem_returns_credential_once_and_stores_digest_only` (grep the store snapshot JSON for the plaintext credential and code: absent), `test_authenticate_requires_matching_device`, `test_authenticate_rejects_revoked_expired_unknown_malformed` (all raise `WorkerAuthError` with the same message), `test_revoke_is_owner_scoped` (other tenant/user cannot revoke), `test_snapshot_restore_roundtrip_keeps_workers`, `test_unknown_harness_rejected`, `test_last_seen_updates`.
- [ ] **Step 2: Run to fail.**
- [ ] **Step 3: Implement** `WorkerRegistry` using `secrets.compare_digest` on digests and `hashlib.sha256`; store mutations under `store._lock` and call `store._after_mutation()`; write audit entries through `store.audit_record(...)` for enroll, redeem, revoke and every auth failure (no secrets in `detail`).
- [ ] **Step 4: Run to pass.**
- [ ] **Step 5: Commit** `feat(assistant-hub): worker registry with digest-only credentials`.

### Task 4: Worker events

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/events.py`
- Modify: `herdr_engineering/control_plane/assistant.py` (store: `worker_events` dict in `__init__`/`snapshot`/`restore`)
- Test: `tests/control_plane/test_assistant_hub_events.py`

**Interfaces:**
- Produces:
  - `@dataclass WorkerEvent`: `event_id, tenant_id, user_id, worker_id, session_id, kind, provenance, payload, created_at`; `kind` in `ask|report|observation`; `provenance` in `worker_reported|harness_attested`.
  - `record_event(store: AssistantStore, worker: Worker, *, kind: str, provenance: str, session_id: str, payload: dict[str, Any]) -> WorkerEvent`; payload passed through `redact_obj`, capped at 16 KiB serialized (larger raises `ValueError`).
  - `list_events(store, *, tenant_id: str, user_id: str, worker_id: str | None = None, kind: str | None = None, limit: int = 100) -> list[WorkerEvent]`.

- [ ] **Step 1: Write failing tests**: `test_record_and_list_scoped_to_tenant_user`, `test_payload_secrets_redacted`, `test_oversize_payload_rejected`, `test_unknown_kind_or_provenance_rejected`, `test_events_survive_snapshot_restore`.
- [ ] **Step 2: Run to fail.**
- [ ] **Step 3: Implement** both functions.
- [ ] **Step 4: Run to pass.**
- [ ] **Step 5: Commit** `feat(assistant-hub): worker event log`.

### Task 5: MCP handler

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/mcp.py`
- Test: `tests/control_plane/test_assistant_hub_mcp.py`

**Interfaces:**
- Consumes: `WorkerRegistry`, `record_event`/`list_events`, `AssistantService` read methods (`list_grants`, `list_approvals`, `search_memory`, `list_routines`, `list_connectors`, `list_delegations`, `list_proposals`, `list_assistants`).
- Produces: `handle(hub: Hub, message: Any, authorization: str, device: str | None) -> tuple[int, dict | None]` where `Hub` is a small dataclass `(registry: WorkerRegistry, service: AssistantService, store: AssistantStore)`; same status/shape conventions as `collab_mcp.handle` (notification -> 202/None, auth failure -> 401 `-32001`, tool errors -> `isError` result). Tool names: `assistant_ask`, `report`, `memory_search`, `grants_list`, `approvals_list`, `routines_list`, `connectors_list`, `delegations_list`, `proposals_list`.

- [ ] **Step 1: Write failing tests** with a real `Hub` built from a real `AssistantService`/`WorkerRegistry`: `test_initialize_and_tools_list`, `test_unauthenticated_401_uniform`, `test_assistant_ask_records_and_returns_no_decision_shadow`, `test_report_records_worker_reported_even_if_payload_claims_attested`, `test_scope_enforced` (worker without `read` cannot call read tools), `test_read_tools_scoped_to_worker_tenant`, `test_connectors_list_never_includes_credentials`, `test_unknown_tool_and_bad_arguments` (extra args rejected as in `collab_mcp`), `test_text_from_outside_is_marked_untrusted` (events listed back carry the `UNTRUSTED` banner).
- [ ] **Step 2: Run to fail.**
- [ ] **Step 3: Implement** `handle` following `collab_mcp.handle`; owner's first non-archived assistant is the target of `memory_search`; tools return `_text(..., untrusted=True)` for anything originating from workers or memory.
- [ ] **Step 4: Run to pass.**
- [ ] **Step 5: Commit** `feat(assistant-hub): MCP handler with ask, report and read tools`.

### Task 6: Hub listener

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/server.py`
- Modify: `herdr_engineering/cli.py` (`control-plane serve` wiring near ~:3419-3441: start `HubServer` when `assistant_config()["hub"]["enabled"]`)
- Test: `tests/control_plane/test_assistant_hub_server.py`

**Interfaces:**
- Produces: `class HubServer(hub: Hub, *, host: str, port: int, allowed_networks, allowed_hosts: list[str], max_body: int = 65536)` with `serve()` (blocking), `start() -> None` (thread), `wait_until_ready(timeout: float = 5.0)`, `shutdown()`, `.port` (bound port; `port=0` allowed for tests). The constructor raises `ValueError` if `bind_allowed(host)` is false.
- Routes: `POST /assistant/mcp` (JSON-RPC via `mcp.handle`), `POST /assistant/enroll` (body `{"code","device_id"}` -> `{"credential","worker_id","device_id","expires_at"}`, uniform 401), `POST /assistant/hook-events` (Authorization `Worker`; body `{"session_id","kind":"observation","payload"}` -> recorded `harness_attested`). Everything else 404.
- Request checks, in order: peer allowed (else 403 uniform, before reading the body), Host allowed, single `Content-Length` and no `Transfer-Encoding`, no `Origin`, `application/json`, body size cap, rate limit per peer.

- [ ] **Step 1: Write failing tests** over real sockets: `test_refuses_wildcard_bind`, `test_mcp_roundtrip_with_enrolled_worker`, `test_peer_outside_allowed_networks_rejected` (server started with `allowed_networks=["100.64.0.0/10"]`; a loopback client gets 403 and the body is never parsed), `test_x_forwarded_for_ignored`, `test_bad_host_header_rejected`, `test_origin_rejected`, `test_ambiguous_framing_rejected`, `test_oversize_body_413`, `test_enroll_code_replay_401`, `test_hook_events_recorded_attested_and_mcp_report_is_not`, `test_unknown_path_404`.
- [ ] **Step 2: Run to fail.**
- [ ] **Step 3: Implement** `HubServer` on `ThreadingHTTPServer` with daemon threads (pattern: `_DaemonThreadingServer`, `api.py:163`); framing/origin/content-type/size checks copied in behavior from `api.py` `_jsonrpc` (`api.py:2163-2200`).
- [ ] **Step 4: Run to pass.**
- [ ] **Step 5: Wire startup** in `cli.py` and add `test_cli_starts_hub_when_enabled` (starts the real serve command in a subprocess with a temp config on port 0 and an `initialize` call; skip only if the serve command cannot run in the test environment, stating why).
- [ ] **Step 6: Commit** `feat(assistant-hub): tailnet-only listener`.

### Task 7: Owner enrollment routes and CLI

**Files:**
- Modify: `herdr_engineering/control_plane/api.py` (`_assistant_post`/`_assistant_get`/delete handling ~:4372-4650: `assistant-workers`)
- Modify: `herdr_engineering/cli.py` (subcommand `control-plane assistant-worker enroll|list|revoke`, modeled on `assistant-audit`, `cli.py:673-681,3007`)
- Test: `tests/control_plane/test_assistant_hub_owner_routes.py`

**Interfaces:**
- Routes (operator API key only): `POST /api/v1/assistant-workers` `{"name","harness"}` -> `{"enrollment_id","code","expires_at","setup":{...}}`; `GET /api/v1/assistant-workers`; `DELETE /api/v1/assistant-workers/<worker_id>`. The `setup` object holds the harness-specific snippet; for `claude` the exact line `claude mcp add --transport http herdr-assistant http://<hub-host>:<port>/assistant/mcp --header "Authorization: Worker <CREDENTIAL>" --header "X-Herdr-Device: <DEVICE>"` with placeholders, plus the `enroll` redeem command. Other harnesses return `{"supported": false}` in Phase 1.
- A worker credential on these routes gets 401/403 (they are not valid API keys).

- [ ] **Step 1: Write failing tests** against a real `ControlPlaneServer` (pattern `test_assistant_api.py`): `test_create_enrollment_requires_operator_key`, `test_worker_credential_cannot_reach_owner_routes`, `test_list_never_returns_secrets`, `test_revoke_then_mcp_401`, `test_feature_disabled_503`, `test_cli_enroll_prints_code_once` (invoke the CLI against the running test server).
- [ ] **Step 2: Run to fail.**
- [ ] **Step 3: Implement** routes and CLI.
- [ ] **Step 4: Run to pass.**
- [ ] **Step 5: Commit** `feat(assistant-hub): owner enrollment routes and CLI`.

### Task 8: Claude Code hook relay

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/hook_relay.py`
- Modify: `herdr_engineering/cli.py` (`assistant-worker hook` and `assistant-worker install-hooks --harness claude --settings PATH [--yes]`)
- Test: `tests/control_plane/test_assistant_hub_hook_relay.py`

**Interfaces:**
- Produces:
  - `relay(event: dict, config: RelayConfig, *, post: Callable | None = None) -> str | None`, where it POSTs an observation to `/assistant/hook-events` over real HTTP and returns the text for stdout. Phase 1 always returns `None` (no decision; the harness's own prompt runs), including when the hub is unreachable (exit 0, nothing printed).
  - `RelayConfig.load(path: Path) -> RelayConfig` reading `{"url","credential","device_id"}` from `$HERDR_ASSISTANT_WORKER_FILE` or `~/.config/herdr/assistant-worker.json` (mode must be 0600 or the relay refuses to send).
  - `install_hooks(settings_path: Path, *, command: str, confirm: Callable[[str], bool]) -> str` returns the unified diff; writes only after `confirm(diff)` is true; adds `PermissionRequest` and `PostToolUse` hook entries without touching other hooks; idempotent.
- The relay persists nothing but an optional diagnostics log; it caches no decisions.

- [ ] **Step 1: Write failing tests**: `test_relay_prints_nothing_for_permission_request`, `test_relay_posts_observation_to_real_hub` (real `HubServer`), `test_relay_unreachable_hub_exits_clean_no_output`, `test_relay_refuses_world_readable_credential_file`, `test_relay_writes_no_decision_files` (temp HOME listing before/after only differs in the log), `test_install_hooks_diff_confirm_and_idempotent`, `test_install_hooks_preserves_existing_hooks`, `test_install_hooks_declined_writes_nothing`.
- [ ] **Step 2: Run to fail.**
- [ ] **Step 3: Implement.** Hook JSON shapes follow the Claude Code hooks reference (`PermissionRequest`, `PostToolUse`); verify field names against the installed Claude Code (2.1.314) with a throwaway-HOME smoke run and record any difference in the test names.
- [ ] **Step 4: Run to pass.**
- [ ] **Step 5: Commit** `feat(assistant-hub): Claude Code hook relay and installer`.

### Task 9: End-to-end smoke and docs

**Files:**
- Create: `tests/control_plane/test_assistant_hub_e2e.py`
- Modify: `docs/superpowers/specs/rakazo-personal-assistant-20261027T193417Z/operator-guide.md` or the repo's current assistant operator guide (find with `grep -rl "Personal Assistant" docs/`), `deploy/control-plane/fleet-config.example.yaml` (commented `hub:` block)

- [ ] **Step 1: Write the failing e2e test**: start a real control plane + hub (port 0, loopback), create an enrollment via the owner API, redeem it, call `initialize`, `tools/list`, `assistant_ask`, `report`, `memory_search` over MCP, post a hook event, then revoke and assert 401; assert the audit chain verifies (`verify_audit`) and contains no plaintext credential.
- [ ] **Step 2: Run to fail**, then fix wiring gaps found.
- [ ] **Step 3: Optional smoke** (`@pytest.mark.integration`): in a throwaway HOME run the installed `claude mcp add` snippet from the enrollment response and `claude mcp list`; skip with an explicit reason when `claude` is absent.
- [ ] **Step 4: Docs**: enabling the hub, binding to the tailnet address, enrolling a Claude Code worker, what Phase 1 does and does not do (records only), the known limit from Review Focus, how to revoke.
- [ ] **Step 5: Run the whole new suite plus** `python3 -m pytest tests/control_plane/test_assistant_service.py tests/control_plane/test_assistant_api.py -q`. Expected: PASS, with no regressions.
- [ ] **Step 6: Commit** `test+docs(assistant-hub): end-to-end smoke and operator docs`.
