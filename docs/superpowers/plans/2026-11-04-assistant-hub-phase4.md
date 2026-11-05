# Assistant hub Phase 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner gives a worker a request and is contacted in Slack only for a real question, a dangerous action not allowed before, or a deploy that is not set to auto; herdr-engineering can be set to auto-deploy per project and scope.

**Architecture:** A Slack runtime (Socket Mode for taps, Web API for messages) is added next to the Jev judge runtime. Taps are recorded through the existing `record_owner_decision` path with a new provenance `owner_slack`. A one-shot approval lets a worker's re-ask become `authorized` once. Deploy permission is a hub-side config block plus a standing grant row; the hub verifies preconditions itself and checks the deploy outcome afterwards.

**Tech Stack:** Python 3, stdlib HTTP plus `slack_sdk` (pinned, Socket Mode), pytest with real local listeners (no mocks), PyYAML config.

**Spec:** `docs/superpowers/specs/2026-11-04-assistant-hub-phase4-design.md` (approved 2026-11-04). The spec wins over this plan.

## Global Constraints

- Package: `herdr_engineering/control_plane/assistant_hub/` (call it **H**), `herdr_engineering/control_plane/assistant.py` (**A**), `herdr_engineering/control_plane/api.py`, `herdr_engineering/cli.py`.
- Never commit to main; TDD; no mocks, stubs or fakes of the unit under test (a local listener that speaks the real protocol, as `tests/control_plane/judge_support.py` does, is how external services are tested).
- Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Implementers run on **sonnet**.
- New snapshot fields and dataclass fields must default, so older snapshots restore.
- Tokens: stdin-only registration, file mode 0600, never logged, never in audit or events, only fingerprints printed.
- Audit names (exact): `assistant.slack.ask_posted`, `assistant.slack.decision`, `assistant.slack.refused`, `assistant.deploy.authorized`, `assistant.deploy.outcome`. Detail is `k=v` ids only.
- Vetoed actions are never posted to Slack. Slack text, emoji and thread replies never authorize; only a verified button tap does.
- Test run (from repo root): `python3 -m pytest tests/control_plane/test_assistant_hub_*.py -q`. If `asyncpg` is missing, install it with `pip install --target <dir> asyncpg` and set `PYTHONPATH=<dir>`. Lint: `ruff check herdr_engineering tests`.
- Rulings (plan-level, recorded here because the spec is silent): (a) an owner's one-shot approval of an exact request skips the shadow check and the judge for that one request but never a veto; (b) the Slack app token and bot token are two files; (c) Slack base URL is configurable and may be loopback `http` (like the judge endpoint) so tests can use a real local listener.

## Review Focus

Each line becomes a test in the task that owns the code.

1. A stale, forwarded, edited or double-tapped Slack message must not approve twice or approve a decided/expired request (Task 3).
2. Slack down, slow or rejecting: the worker still gets `pending` promptly and nothing is lost; no tap, no authorization (Task 4).
3. Slack message text containing mrkdwn control sequences (`<!channel>`, `<@U..>`, links, backticks) from a worker-controlled `display` must render inert (Task 4).
4. Deploy argv variants that must NOT match: `--ref=<sha>`, `--ref main`, short sha, extra args after the sha, a different script path, a chain (`&& `), a wrapper (`env`, `ssh`) (Task 7).
5. Hub restarted (container recreated) mid-deploy: the deploy check resumes from the store and still reports; an unreachable health endpoint ends as `unconfirmed`, never success (Task 9).
6. One chatty worker or task must not flood the owner (Tasks 4 and 6).

---

### Task 1: Slack config, token registration CLI, status

**Files:**
- Create: `H/slack_register.py`, `tests/control_plane/slack_support.py` (SlackListener), `tests/control_plane/test_assistant_hub_slack_register.py`, `tests/control_plane/test_assistant_hub_slack_config.py`
- Modify: `A` (`assistant_config`, new `_slack_config`), `herdr_engineering/cli.py` (parser + handler), `pyproject.toml` (add pinned `slack_sdk`), `deploy/control-plane/fleet-config.example.yaml` (commented `slack:` block)

**Interfaces:**
- Produces: config block `personal_assistant.slack` = `{enabled: bool=False, base_url: str="https://slack.com/api", bot_token_file: abs path, app_token_file: abs path, owner_user_id: str (Slack user id, ^U[A-Z0-9]+$), channel: str=owner DM (resolved at runtime), ask_ttl_minutes: int 1..120 default 15, max_asks_per_task_per_hour: int default 6}`; `_slack_config(raw) -> dict` (same style as `_judge_config`: unknown keys raise, inline secrets `bot_token|app_token|token|secret` raise, files absolute).
- Produces: `register_tokens(base_url: str, bot_file: str, app_file: str, bot_token: str, app_token: str) -> SlackRegistered(bot_fingerprint, app_fingerprint, team, bot_user_id)`; `SlackRegisterError(code)` codes `bad_token`, `config`, `bad_path`, `unsafe_dir`, `symlink`, `io`, `auth_failed`.
- Produces: CLI `herdr-eng control-plane assistant-slack register|status` (register reads both tokens from stdin only, one per line, bot first; prints fingerprints only).
- Consumes: the key-file write helpers pattern in `H/judge_register.py` (`_check_target`, `_write_new`, `fingerprint`) — reuse by importing, do not copy.

- [ ] **Step 1:** Write failing tests: `_slack_config` accepts a valid block, rejects unknown keys, inline tokens, relative paths, non-loopback `http`, bad `owner_user_id`; `register_tokens` against `SlackListener` (real local HTTP server in `slack_support.py` serving `auth.test` for a good bot token and 401-style `{"ok":false,"error":"invalid_auth"}` otherwise) writes both files 0600 via O_EXCL, rotates through `PATH.new`, refuses group-writable parent dir and symlinks; the CLI refuses without stdin tokens and prints only fingerprints.
- [ ] **Step 2:** Run them; expected FAIL (module missing).
- [ ] **Step 3:** Implement `_slack_config`, `H/slack_register.py`, CLI wiring (`assistant-slack` parser next to `assistant-judge`, `status` calls a new GET route added in Task 4), and `SlackListener` (threaded real HTTP server on 127.0.0.1 port 0, records raw requests, serves `auth.test`).
- [ ] **Step 4:** Run tests plus `ruff`; expected PASS.
- [ ] **Step 5:** Commit `feat(assistant-hub): Slack config and token registration`.

### Task 2: `owner_slack` provenance and one-shot approval

**Files:**
- Modify: `H/policy.py` (`record_owner_decision`, `decide`), `H/actiongrants.py` (`ActionRequestRow.consumed_at`), `H/evidence.py` (`count_qualifying`)
- Test: `tests/control_plane/test_assistant_hub_one_shot.py`

**Interfaces:**
- Produces: `record_owner_decision(store, cfg, now, tenant_id, user_id, request_id, approve, decided_by, *, judge_version="", provenance="owner_api")` where `provenance in {"owner_api","owner_slack"}` (else `ValueError`); `decided_by` for Slack is `slack:<user_id>`.
- Produces: `ActionRequestRow.consumed_at: datetime | None = None`; config `grants.approval_ttl_minutes` (int, default 15, 1..120) validated in `_grants_config`.
- Produces: in `decide`, after vetoes and before shadow/judge: if an `approved` row exists for the same tenant, user, worker and `key_id`, decided within `approval_ttl_minutes`, `consumed_at is None`, then set `consumed_at=now` (under the store lock), audit `assistant.action.authorized`, and return `Decision(authorized, source="owner", request_id=<that row>)`. A second identical request is `pending` again.
- Consumes: `count_qualifying` counts `owner_api` today; it must count both provenances.

- [ ] **Step 1:** Failing tests: approve via `owner_slack` counts toward `propose_if_ready` exactly like `owner_api`; an unknown provenance raises; approve then re-ask returns `authorized` once, the next re-ask is `pending`; a different worker or different `key_id` does not consume it; an approval older than the TTL does not authorize; a vetoed command stays `denied` even with an approved row; a denial is not consumable.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement the keyword param, the new field (default `None`, snapshot round-trips), the consume step (atomic under `store._lock`), config validation.
- [ ] **Step 4:** Run the new tests and the existing `test_assistant_hub_phase2_*` and `phase3_*` tests; expected PASS.
- [ ] **Step 5:** Commit `feat(assistant-hub): owner_slack provenance and one-shot approvals`.

### Task 3: Slack asks store and tap handling

**Files:**
- Create: `H/slack_asks.py`, `H/slack.py` (`SlackClient`, `SlackRuntime`), `tests/control_plane/test_assistant_hub_slack_taps.py`
- Modify: `A` (new `slack_asks` collection: init, index, cap, `snapshot`, `restore`, accessors), `tests/control_plane/slack_support.py` (add a Socket Mode WebSocket endpoint and `chat.postMessage`/`chat.update`/`apps.connections.open`)

**Interfaces:**
- Produces: `@dataclass SlackAsk(ask_id, tenant_id, user_id, kind: "action"|"question"|"summary"|"deploy", request_id="", task_id="", nonce, channel="", ts="", status: "open"|"decided"|"expired", remember: bool=False, answer: str|None=None, created_at, expires_at)` (all but ids defaulted); store accessors `put_slack_ask`, `get_slack_ask(ask_id, *, tenant_id, user_id)`, `list_slack_asks(...)`, cap constant `max_slack_asks_per_tenant = 2000` using `_bounded_put`.
- Produces: `SlackClient(cfg_getter)` with `post_message(channel, blocks, text) -> ts`, `update_message(channel, ts, blocks, text)`, `open_dm(user_id) -> channel`; built per call from live config so token rotation applies; deadline-bounded like `JevClient`.
- Produces: `SlackRuntime(store, service, cfg_getter, now_fn)` with `start()`, `stop()`, `handle_interaction(payload: dict) -> None`. A tap is acted on only if: payload user id equals configured `owner_user_id`; the `ask_id` and `nonce` in the button value match an `open` ask; it is not expired; the message `channel`/`ts` equal the stored ones. Otherwise audit `assistant.slack.refused` and change nothing. On a valid tap call `record_owner_decision(..., provenance="owner_slack", decided_by="slack:<uid>")`, set the ask `decided`, update the message (buttons removed, outcome shown), audit `assistant.slack.decision`.

- [ ] **Step 1:** Failing tests against `SlackListener`: a valid Approve tap approves the request and removes the buttons; Deny denies; wrong user id, wrong nonce, already-decided, expired, wrong channel/ts, and a tap on a vetoed request all change nothing and audit `refused`; a double tap decides once; asks survive `snapshot`/`restore`.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement the collection, client and runtime. Socket Mode via `slack_sdk` against the listener's `apps.connections.open` URL; if `slack_sdk` refuses a loopback `ws://` URL, the test listener serves `wss` with a self-signed cert and the runtime accepts an optional `ca_file` config key (add it to `_slack_config`).
- [ ] **Step 4:** Run tests plus `ruff`; expected PASS.
- [ ] **Step 5:** Commit `feat(assistant-hub): Slack ask store and verified taps`.

### Task 4: Post asks for pending actions; status route; wiring

**Files:**
- Modify: `H/policy.py` (`_pending` notify hook), `H/slack.py` (`notify_pending`), `H/mcp.py` (`Hub.slack` field), `herdr_engineering/cli.py` (`_start_assistant_hub` builds and starts/stops the runtime, `_wire_assistant_hub` sets `svr.assistant_slack`), `api.py` (new resource `assistant-slack` GET status, wired in all five places listed in the interface map, rate limited), tests
- Test: `tests/control_plane/test_assistant_hub_slack_asks.py`

**Interfaces:**
- Produces: `SlackRuntime.notify_pending(row: ActionRequestRow, display: str, why: str) -> None` — called only when `_pending` created a new row (`created` is true), never for `vetoed`, never from the request thread's critical path (enqueue to a bounded drop-oldest queue drained by the runtime thread, like `ShadowRecorder`). Message: worker, project (if known), `display` made inert, reason, buttons **Approve / Deny / Approve and remember** whose values carry `ask_id` and `nonce`. Audit `assistant.slack.ask_posted`.
- Produces: rate limit `max_asks_per_task_per_hour` per (worker, task): over the limit the ask is not posted individually; one batched line "N more waiting" is posted instead.
- Produces: `GET /api/v1/assistant-slack` (operator) returning `{enabled, connected, last_event_at, open_asks}`; no tokens.
- Inertness helper `slack_text(s: str) -> str`: escapes `& < >`, neutralizes `@channel`/`@here`/user and link syntax, strips control characters, truncates to 300.

- [ ] **Step 1:** Failing tests: a new pending action posts exactly one message with three buttons; re-asking the same pending request posts nothing new; a veto posts nothing; Slack unreachable (closed port) leaves `decide` returning `pending` quickly and the ask is retried by the runtime once reachable; `<!channel>`, `<@U123>`, `<https://x|y>` and backticks in `display` arrive inert; the per-task limit batches; status route requires the operator key and never returns token text.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement hook, queue, helper, route, wiring.
- [ ] **Step 4:** Run tests plus `ruff`; expected PASS.
- [ ] **Step 5:** Commit `feat(assistant-hub): post owner asks to Slack`.

### Task 5: Questions, answers and approve-and-remember

**Files:**
- Modify: `H/mcp.py` (`_ask`, `_TOOLS` schema), `H/slack.py` (`post_question`, thread-reply handling, `remember`), `H/policy.py` (helper), tests
- Test: `tests/control_plane/test_assistant_hub_slack_questions.py`, `tests/control_plane/test_assistant_hub_slack_remember.py`

**Interfaces:**
- Produces: `assistant_ask` takes optional `ask_id`. Without it: creates a `question` `SlackAsk`, posts it, returns `{"status":"pending","ask_id":...}`. With it: returns `{"status":"pending"}` or `{"status":"answered","answer":<text>}` or `{"status":"expired"}`, scoped to the calling worker's tenant and user. The owner's threaded text reply (from the configured owner id only) sets `answer` (capped 4 KB, treated as untrusted data for the worker, never authorization).
- Produces: **Approve and remember** records the approval (as in Task 3), sets `remember=True`, then calls `activate_grant(...)` if `propose_if_ready` returned a PROPOSED grant. `ActivationRefused.code` is posted in the thread in plain words (what is missing). If refused, the intent is kept: after any later `owner_slack` approval for the same worker and `key_id`, activation is attempted again. No polling.
- Consumes: `activate_grant(store, cfg, now, tenant_id, user_id, grant_id, confirmed_by, judge)`; `confirmed_by="slack:<uid>"`.

- [ ] **Step 1:** Failing tests: question posts once and the answer round-trips through a thread reply; a reply from another user is ignored; an expired question answers `expired`; remember with all gates satisfied (use the existing `satisfy_gate` helper) activates the grant and the next request is `authorized` via the grant; remember while still in shadow records the approval, posts the missing-gate reason, and later activates on the next qualifying approval; a worker cannot read another tenant's `ask_id`.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run tests plus the existing phase 1 and 3 ask/judge tests; expected PASS.
- [ ] **Step 5:** Commit `feat(assistant-hub): Slack questions and approve-and-remember`.

### Task 6: Task summaries

**Files:**
- Modify: `H/mcp.py` (`_report` accepts `task_id: str` (<=128) and `final: bool`; schema stays `additionalProperties:false`), `H/slack.py` (`post_summary`), `H/events.py` if the payload shape needs it, tests
- Test: `tests/control_plane/test_assistant_hub_slack_summary.py`

**Interfaces:**
- Produces: a `report` with `task_id` and `final: true` triggers one summary per (worker, task_id): counts of actions authorized under grants, one-shot approvals, denied and vetoed items, any deploy outcome (Task 9), and the worker's report text (untrusted, made inert, capped). A task with no final report and no event for 30 minutes (`summary_idle_minutes`, default 30, in `_slack_config`) gets the summary from the runtime's timer. A task never gets two summaries. Quiet work posts nothing else.
- Consumes: `ActionRequestRow.task_id`, `WorkerEvent` records.

- [ ] **Step 1:** Failing tests: final report posts exactly one summary with correct counts; repeated final reports do not repeat it; idle timeout posts one; a chatty worker (many non-final reports) posts nothing; `report` without the new args still works.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement. Summary state lives in a `SlackAsk(kind="summary", task_id=...)` row so restarts do not repeat it.
- [ ] **Step 4:** Run tests; expected PASS.
- [ ] **Step 5:** Commit `feat(assistant-hub): end-of-work Slack summary`.

### Task 7: Deploy config, deploy matching and standing grants

**Files:**
- Create: `H/deploy.py`, `tests/control_plane/test_assistant_hub_deploy_config.py`, `tests/control_plane/test_assistant_hub_deploy_match.py`
- Modify: `A` (`_deploy_config`), `H/canonical.py` (derive pre-step), `H/actiongrants.py` (`ActionGrant.source: str = ""`), `H/policy.py` (`create_standing_grant`), `H/slack.py` (confirmation ask), docs example in `fleet-config.example.yaml`

**Interfaces:**
- Produces: config `personal_assistant.deploy.projects.<id> = {repo: "owner/name", scopes: {<scope>: {mode: "ask"|"auto" (default ask), command: relative path, require: subset of [on_main, ci_green, descends_from_deployed]}}}`; `mode: auto` in YAML is only a *candidate*: it takes effect only when a standing grant exists (below). `_deploy_config(raw) -> dict`.
- Produces: `match_deploy(raw: RawAction, deploy_cfg: dict) -> DeployMatch | None`; `DeployMatch(project, scope, repo, sha, argv_digest)`. Matches only a single simple command whose argv is exactly `[<command>, "--ref", <40 lowercase hex>]` with `raw.repo` equal to the configured repo (a disagreeing hint makes it owner-decides, per the hint rule). Wired as a pre-step in `derive`, emitting `mk_key("deploy", Verb.DEPLOY_PROD, <repo>, argv_digest(argv))` so the key binds the sha.
- Produces: `create_standing_grant(store, cfg, now, tenant_id, user_id, worker_id, key: ActionKey, confirmed_by: str, judge_version: str) -> ActionGrant` — ACTIVE immediately, `source="owner_standing"`, `confirmed_by="slack:<uid>"`, `expires_at=now+grant_ttl_days`. Skips only approval-count and diversity rules; `effective_state`, shadow, judge gate (`_check_judge_gate` must pass at creation, same codes) and vetoes still apply. A standing grant for a deploy matches a *scope*, not one sha: the grant's `key_canonical` is the scope key with `target=""`; `_live_grant` for deploy matches on (worker, verb, repo, scope) and the sha is checked by Task 8.
- Slack confirmation: owner sets auto through `POST /api/v1/assistant-deploys/<project>/<scope>/auto` (operator) which posts a confirmation ask; the standing grant is created only on the owner's tap.

- [ ] **Step 1:** Failing tests for the Review Focus 4 argv variants (each is a non-match, ends owner-decides as `unknown` like today); the exact form matches and yields a sha-bound key; config validation rejects bad repo/scope/mode/require; no standing grant exists without the tap; revoking the standing grant returns to `pending`; a judge-gate refusal at creation is reported to the owner.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run tests plus the existing canonical and veto suites (deploy.sh must not be newly vetoed); expected PASS.
- [ ] **Step 5:** Commit `feat(assistant-hub): per-scope deploy permission`.

### Task 8: Hub-side deploy preconditions

**Files:**
- Create: `H/preconditions.py`, `tests/control_plane/test_assistant_hub_preconditions.py`
- Modify: `herdr_engineering/control_plane/review_gate/github_app.py` (new `commit_status(repo, sha)` and `check_runs(repo, sha)`), `herdr_engineering/ci.py` (tokenless `WoodpeckerProvider.public_pipelines(repository)`), `H/policy.py` (`_decide` consults preconditions for deploy keys)

**Interfaces:**
- Produces: `check_preconditions(match: DeployMatch, scope_cfg: dict, gh, ci, health) -> PreconditionResult(ok: bool, failed: tuple[str, ...], unchecked: tuple[str, ...])`; `ok` only if every name in `require` is verified: `on_main` (`GitHubAppClient.default_branch_head(repo)` equals the sha), `ci_green` (combined status and check runs for the sha are all success AND the Woodpecker pipeline with `revision == sha` is successful), `descends_from_deployed` (deployed commit from `GET /api/v1/health?probe=1` `commit`; `commits_between(repo, deployed, sha)` shows the sha ahead and not behind). Any error, timeout or missing data is `unchecked`, which is not ok. Every network call is deadline-bounded.
- `_decide` for a matched deploy with a live standing grant: `ok` -> authorized (audit `assistant.deploy.authorized` with project, scope, sha) and a deploy check is created (Task 9); not ok -> `pending` with reason naming the failed or unchecked preconditions, and the owner is asked via Task 4.

- [ ] **Step 1:** Failing tests using real local HTTP listeners standing in for GitHub, Woodpecker and health (as in `judge_support.py`): all green -> ok; tip moved -> `on_main` failed; red or pending CI -> `ci_green` failed; sha behind deployed -> failed; each endpoint down -> `unchecked` and `pending`; a worker-supplied claim of "ci is green" changes nothing.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement the two new client methods and the evaluator.
- [ ] **Step 4:** Run tests plus existing review_gate and ci tests; expected PASS.
- [ ] **Step 5:** Commit `feat(assistant-hub): hub-verified deploy preconditions`.

### Task 9: Deploy outcome check

**Files:**
- Create: `H/deploycheck.py`, `tests/control_plane/test_assistant_hub_deploycheck.py`
- Modify: `A` (new `deploy_checks` collection: same persistence steps as `slack_asks`), `H/slack.py` (`post_deploy_outcome`), `herdr_engineering/cli.py` (runtime resumes open checks at start), `H/preconditions.py` (shared health client)

**Interfaces:**
- Produces: `@dataclass DeployCheck(check_id, tenant_id, user_id, project, scope, sha, request_id, status: "awaiting"|"healthy"|"rolled_back"|"failed_rollback"|"unconfirmed", started_at, deadline, last_error="")`. `verify_deploy(store, check, health, now) -> DeployCheck`: polls `/api/v1/health?probe=1` (tolerating connection failure while the hub's own container is replaced) until `commit` startswith the sha (healthy), or a different earlier commit is back after the deploy window (rolled_back), or the deadline (config `deploy_check_minutes`, default 20) passes (unconfirmed). `failed_rollback` is set only when the worker reports exit 3 in its `report` (`kind="outcome"`, payload `{"deploy_exit": 3}`) and is posted as an urgent ask that is never rate-limited or batched.
- The Slack outcome message and the Task 6 summary both include the final status. A deploy with no confirmed result is worded "unconfirmed", never "success".

- [ ] **Step 1:** Failing tests with a real local health listener whose reported commit changes mid-test: healthy; rollback; deadline -> unconfirmed; listener down for a while then up -> still healthy; the runtime restarted (new `SlackRuntime` over the restored snapshot) resumes an `awaiting` check; exit-3 report posts the urgent message; checks are tenant-scoped.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run tests; expected PASS.
- [ ] **Step 5:** Commit `feat(assistant-hub): independent deploy outcome check`.

### Task 10: Docs and end-to-end

**Files:**
- Modify: `docs/operations/assistant-hub.md` (Phase 4 section: Slack setup and manifest, token registration, how taps count as owner evidence and the widened trust boundary, one-shot approvals, deploy scopes and `mode`, preconditions, advisory limit, what is not verified), replace the "Phase 4 adds ..." sentence in "Why nothing is autonomous yet"
- Create: `tests/control_plane/test_assistant_hub_phase4_e2e.py`

- [ ] **Step 1:** Write the e2e: enroll a worker, register Slack tokens, worker `action_request` for an unknown command -> Slack ask -> Approve tap -> re-ask `authorized` once; question -> thread answer; Approve-and-remember with the gate satisfied -> later request authorized without Slack; deploy scope: confirm auto by tap -> matched deploy with green listeners authorized -> health listener shows the new commit -> summary posted with `healthy`; the same flow with red CI ends `pending` and asks.
- [ ] **Step 2:** Run; expected FAIL until docs-independent code passes, then PASS.
- [ ] **Step 3:** Write the docs, including an explicit "Not verified" list (real Slack workspace, real herdr-engineering deploy, Socket Mode against production Slack).
- [ ] **Step 4:** Full suite and lint: `python3 -m pytest -q` (about 5 minutes) and `ruff check herdr_engineering tests`; expected PASS.
- [ ] **Step 5:** Commit `docs(assistant-hub): Phase 4 operations guide and e2e`.
