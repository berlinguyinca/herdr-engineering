# Assistant Hub Phase 3 Implementation Plan (Jev judge: client, config, key registration, shadow recorder, calibration gate)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Jev (InferWeave System One) judge to the hub: a typed client for the real `POST /v1/systemone` contract, a strict `judge:` config section, key registration that never echoes the key, a shadow recorder that stores what the judge would have said next to what the owner decided, and a calibration report whose gate is the thing that finally lets `activate_grant` succeed. The judge can only add friction; it never lowers a requirement.

**Architecture:** Pure modules in `assistant_hub/` (`judge_config` lives in `assistant.py` with the other validators; `jev.py` client, `judge.py` question set + verdict + gate, `calibration.py` math) over one new store collection (`judge_records`). `policy.decide` consults the judge only on the path that would otherwise return `authorized` (an ACTIVE grant) and fails closed; an async single-thread recorder writes shadow records for pending requests so a slow judge never blocks `action_request`. The activate route's hard-wired `judge_gate_satisfied=False` is replaced by the real gate, which stays false until the owner confirms a real model is serving AND enough labeled decisions exist with a bounded false-negative rate.

**Tech Stack:** Python 3.12 stdlib only (`urllib`/`http.client`, `threading`, `math`, `hashlib`), existing `AssistantStore`/`Hub`/`ControlPlaneServer`, pytest with real objects (no mocks, no fake Jev server).

**Spec:** `docs/superpowers/specs/2026-10-30-assistant-claude-mcp-and-jev-design.md` sections 5.3 (`judge_status`), 6.2-6.3, 7 (7.1 config, 7.2 key registration, 7.3 client and question set, 7.4 shadow recording and calibration gate), 8, 13 (ground truth for the calibration gate). Phase 1 PR 537, Phase 2 PR 541 are merged.

## Facts established by the survey (these override the spec where they differ)

- **Real request contract** (InferWeave `docs/system-one/api.md`, `crates/iw-systemone/src/schema.rs`): `POST /v1/systemone`, `Authorization: Bearer <key>`, JSON `{"state": <string|object|array>, "model": "<id>", "questions": {"<name>": {"type": "noul"|"score"|"choice", "instructions"?: ..., "criteria": ...}}}`. `score.criteria` is an ARRAY of level descriptions (index = level); `choice.criteria` is a non-empty MAP name->description (max 256); `noul.criteria` is optional. Body max 1 MiB. The spec's "noul/score/choice with options" shorthand maps onto this.
- **Real response**: `{"model": str, "answers": {name: {"type":"noul","noul":P(yes)} | {"type":"score","score":expected_level,"confidence":..,"legend":{"0":..},"probabilities":{"0":..}} | {"type":"choice","choice":name,"confidence":..,"probabilities":{..}}}, "usage": {"input_tokens":int,"output_tokens":int}}`. No backend/version field in the body; metadata only in headers (`X-InferWeave-Request-ID`, `X-InferWeave-Model`, `X-InferWeave-Node`).
- **Errors:** 401 `{"error":{"code":"unauthorized",..}}`; 422 FastAPI-style `{"detail":[{"loc":[..],"msg":..,"type":..}]}`; runtime errors `{"error":{"code":"system_one.<x>","message":..}}` with 404/422/429/500/502/503/504. Production (`llm.metabolomics.us`, Go gateway) may return its own inference-error envelope. All of these map to `JudgeUnavailable` (fail closed); the client never inspects error text beyond a short code.
- **No genuinely recorded real Laya response exists** in the repos. The documented example in `api.md` lines 123-133 is the only literal sample; tests label it "documented", not "captured".
- **Mock vs real cannot be told from the response body.** `GET /v1/models` shows `x_backend: "laya"` even for the mock. The reliable signals are `GET /v1/systemone/runtimes` (registered workers) and the operator's knowledge. Therefore the gate requires an explicit owner flag `judge.backend_confirmed: true` in addition to calibration. Production model ids may be canonical checkpoint ids, not `laya-auto`, so `model` is config and registration verifies the id appears in `GET /v1/models`.
- **Latency:** CPU Laya is ~1.3 states/s per node; default client timeout is the spec's 5 s for the authorize path; recorder calls may use a longer timeout (30 s) off the request path.

## Global Constraints

- Branch `feat/assistant-hub-phase3` off origin/main (6b3626ad); never commit to main; plain `git commit`/`git push` (no rebase/force-push); no merge without the owner's word. Commit trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not run repo-wide formatters; `ruff check` only touched files.
- TDD (red run first), no mocks/stubs/fakes, no fake Jev server. Network tests: unreachable/closed port and bad URL (real failures) always run; tests against a real System One mock (`inferweave-systemone-worker` with `INFERWEAVE_SYSTEM_ONE_BACKEND=mock`, or any URL in env `HERDR_JEV_TEST_URL` + key in `HERDR_JEV_TEST_KEY_FILE`) carry `@pytest.mark.integration` and skip with an explicit reason when absent. Tests never touch real `~/.claude`, `~/.codex`, `~/.pi`, `~/.config/herdr`, and never contact production.
- `/api/v1` tests need `asyncpg`: `PYTHONPATH=/tmp/claude-1000/-home-user-IdeaProjects/a8939f5b-bb45-4f5f-a41d-65023acf9586/scratchpad/deps`.
- The API key is read from a file path only (`api_key_file`), never inline in YAML, never in audit/logs/snapshot/MCP output/exception text/`repr`. Only a short fingerprint (first 8 hex of sha256) may be shown.
- Judge input is the canonical `ActionKey` facts (tool, verb, resource_scope, flag_class, risk_class) plus owner-authored context only. Command text, `display`, tool output, PR bodies, chat text, worker hints and `argv` digests/targets are NEVER sent to the judge.
- Fail closed everywhere: judge disabled, unconfigured, unreachable, slow, malformed, empty model id => no autonomy (a resolved checkpoint id that differs from the configured model is expected and recorded, not an error) (`pending`). The judge can only escalate; it never turns `pending` into `authorized`.
- Judge version = sha256 of (model id, thresholds, question-set version); any change resets ACTIVE/PROPOSED grants to SHADOW and invalidates calibration labels of the old version (spec 6.2).
- Defaults (spec 7.1, builder's calls): `timeout_seconds: 5`, `destructive: 0.30`, `irreversible: 0.30`, `severity_score: 2.0`, `min_labeled_decisions: 200`, `max_false_negative_bound: 0.05` (95% one-sided Clopper-Pearson upper bound, per risk class), `backend_confirmed: false`, `model: laya-auto`.
- The canary prompt (spec 7.4, marked droppable) is NOT built in this phase; documented as deferred.

## Review Focus

1. A malicious or compromised judge endpoint (response claims huge probabilities, NaN, negative, wrong types, extra keys, oversize body, slow-drip, redirects to another host, TLS errors) must never crash the hub, hang a request past the timeout, or lower a requirement (Task 2, 5).
2. Prompt/data injection: nothing worker-authored reaches the judge request; assert on the serialized request body (Task 3).
3. The gate must be impossible to satisfy by accident: mock backend, owner flag false, too few labels, labels from an older judge version, labels for a different risk class, label farming by one session (Task 6, 8).
4. Secret handling for the key file: mode 0600 enforced on read (refuse group/world-readable), no key in any output, rotation keeps the old file until the new key passes the health check (Task 7).
5. Recorder load: bounded queue, drop-oldest with a counter, no store lock held while calling the judge, no unbounded thread growth (Task 4).

---

### Task 1: `judge:` config section

**Files:**
- Modify: `herdr_engineering/control_plane/assistant.py` (add `_judge_config` next to `_grants_config`; add `"judge"` to `assistant_config`'s result)
- Test: `tests/control_plane/test_assistant_hub_judge_config.py`

**Interfaces:**
- Produces: `assistant_config(cfg)["judge"]` dict with keys and defaults `enabled: False`, `provider: "jev"`, `endpoint: ""`, `model: "laya-auto"`, `api_key_file: ""`, `timeout_seconds: 5`, `fail_mode: "closed"`, `backend_confirmed: False`, `thresholds: {destructive: 0.30, irreversible: 0.30, severity_score: 2.0}`, `calibration: {min_labeled_decisions: 200, max_false_negative_bound: 0.05}`. Validation like `_hub_config`: unknown keys raise `ValueError("control_plane.personal_assistant.judge has unknown keys: [...]")`; `enabled`/`backend_confirmed` real bools; `provider` only `"jev"`; `fail_mode` only `"closed"`; when `enabled` is true, `endpoint` must be an `https://` URL (or `http://` only for loopback hosts `127.0.0.1`, `::1`, `localhost`) without userinfo, query or fragment, and `api_key_file` an absolute path; `timeout_seconds` int 1..60; thresholds floats in (0,1] for destructive/irreversible and 0..3 for severity_score; `min_labeled_decisions` int >= 20; bound float in (0,0.5]; inline keys named `api_key`/`key`/`token`/`secret` raise with a message pointing to `api_key_file`.
- Produces: `judge_version(judge_cfg) -> str` (sha256 hex[:16] of canonical JSON of model, thresholds, `QUESTION_SET_VERSION`), importable from `assistant_hub/judge.py` later (define the function in `assistant_hub/judge_version.py` now, `QUESTION_SET_VERSION = 1`).

- [ ] **Step 1:** Failing tests: defaults; overrides; unknown key; non-mapping; bool-as-int rejected; enabled without endpoint/key file rejected; `http://` to a non-loopback host rejected; userinfo/query in endpoint rejected; relative `api_key_file` rejected; inline `api_key` rejected with a pointer message; threshold and timeout bounds; `judge_version` changes when model, any threshold, or the constant changes and is stable otherwise.
- [ ] **Step 2:** Run `python3 -m pytest tests/control_plane/test_assistant_hub_judge_config.py -q`; expect failure.
- [ ] **Step 3:** Implement `_judge_config` and `judge_version` following the `_grants_config`/`_hub_config` style.
- [ ] **Step 4:** Pass; ruff on touched files; run `test_assistant_hub_*.py`, `test_assistant_service.py`, `tests/test_central_config_servers.py` for regressions.
- [ ] **Step 5:** Commit `feat(assistant-hub): judge config section and judge version`.

---

### Task 2: Jev client (types, request builder, response validation, transport)

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/jev.py`
- Test: `tests/control_plane/test_assistant_hub_jev.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class NoulAnswer: p_yes: float`; `ScoreAnswer: expected: float; confidence: float; probabilities: Mapping[str, float]`; `ChoiceAnswer: choice: str; confidence: float; probabilities: Mapping[str, float]`; `@dataclass(frozen=True) class JudgeResult: model: str; answers: Mapping[str, NoulAnswer | ScoreAnswer | ChoiceAnswer]; input_tokens: int; output_tokens: int; request_id: str`; `class JudgeUnavailable(Exception)` with a short `code` attribute (`"timeout"`, `"connect"`, `"http_<status>"`, `"malformed"`, `"too_large"`, `"auth"`, `"model_mismatch"`, `"config"`) and NO response-body text in `str()`.
- Produces: `build_request(model: str, state: object, questions: Mapping[str, Mapping]) -> bytes` (UTF-8 JSON, raises `ValueError` if > 256 KiB or empty questions); `parse_response(body: bytes, expected: Mapping[str, str]) -> JudgeResult` where `expected` maps question name -> kind and the parse enforces: body <= 64 KiB, valid JSON object, `answers` has exactly the expected names with the expected `type`, every probability finite and within [0,1], noul within [0,1], score probabilities keys match the question's level count and sum within 1.0 +/- 0.05, `usage` ints >= 0, no extra answer names (extra keys inside an answer are ignored). Anything else raises `JudgeUnavailable("malformed")`.
- Produces: `class JevClient`: `__init__(self, endpoint: str, api_key_file: str, model: str, timeout_seconds: float)`; `health() -> list[str]` (GET `<base>/v1/models`, returns model ids, requires 200 and an OpenAI-shaped `{"data":[{"id":..}]}`); `ask(state, questions: Mapping[str, Mapping], *, timeout: float | None = None) -> JudgeResult`. Key read from file on every call (so rotation works) after checking the file is a regular file owned by the current user with mode `& 0o077 == 0` (else `JudgeUnavailable("config")`); `Authorization: Bearer` header; `Accept: application/json`; no redirects followed (a 3xx is `http_3xx`); response size read capped at 64 KiB + 1; `User-Agent: herdr-assistant-judge/1`; TLS verification on (default context) for https; connection per call (no pooling in v1 - the 5 s budget and a single worker make it unnecessary; note in docstring); the whole call bounded by a monotonic deadline (connect + read per-chunk), so a slow-drip response cannot exceed `timeout`. The 401/422/429/5xx bodies are never copied into exceptions or logs.

- [ ] **Step 1:** Failing tests: `parse_response` accepts the DOCUMENTED example from InferWeave `api.md` (literal JSON in the test, labeled "documented sample, not captured"); rejects: wrong type, missing/extra answer names, NaN/inf/negative/>1 values, probabilities not summing to ~1, level-count mismatch, non-object, oversize, invalid JSON, duplicate `answers` keys (use a raw string with duplicates), `usage` negative. `build_request` shape for noul/score/choice per the real contract. Client tests with REAL failures only: unreachable port => `connect`; bad scheme/URL => `config`; key file missing / world-readable / symlink / wrong owner => `config`; a real local `socket` listener that accepts and never answers => `timeout` within `timeout + 0.5 s`; a listener that returns a 302 => `http_302`; one that streams > 64 KiB => `too_large`; one that drips one byte per 0.2 s => bounded by the deadline. (These listeners are raw sockets in the test process, not a fake Jev: they exist to produce hostile TRANSPORT behavior, and the file docstring says so.) Integration test (`@pytest.mark.integration`, skipped with reason when `HERDR_JEV_TEST_URL` is unset): `health()` lists the model, `ask` returns a structurally valid `JudgeResult` for one question of each kind.
- [ ] **Step 2:** Run; fail.
- [ ] **Step 3:** Implement with `http.client` + `ssl`; no third-party dependency.
- [ ] **Step 4:** Pass; ruff.
- [ ] **Step 5:** Commit `feat(assistant-hub): Jev System One client with strict response validation`.

---

### Task 3: Question set and verdict (pure)

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/judge.py`
- Test: `tests/control_plane/test_assistant_hub_judge.py`

**Interfaces:**
- Consumes: `ActionKey`, `RiskClass`, `JudgeResult`, `JevClient`, `judge_version`.
- Produces: `QUESTION_SET_VERSION = 1` (re-exported); `SEVERITY_LEVELS = ("informational", "minor", "major", "critical")`; `build_state(key: ActionKey, owner_context: Mapping[str, str]) -> dict` containing ONLY `{"action": {"tool","verb","resource_scope","flag_class","risk_class"}, "owner_context": {...}}`; `owner_context` keys are limited to a fixed allowlist (`"note"`) with values <= 500 chars, and Phase 3 passes an empty context (owner-authored context is an extension point, not a data path from workers); `QUESTIONS` dict with the four spec questions: `destructive` (noul), `irreversible_or_external` (noul), `severity` (score with `criteria = list(SEVERITY_LEVELS)` described as plain sentences), `owner_approval_covers` (noul) each with fixed `instructions` strings in the module (no f-strings with variable data).
- Produces: `@dataclass(frozen=True) class Verdict: escalate: bool; reasons: tuple[str, ...]; destructive: float; irreversible: float; severity: float; covers: float; judge_version: str; model: str`; `evaluate(result: JudgeResult, thresholds: Mapping[str, float], version: str) -> Verdict` escalating when destructive >= thresholds.destructive, irreversible >= thresholds.irreversible, severity expected >= thresholds.severity_score, or `covers` < 0.5 (reasons are static strings); `consult(client: JevClient, key: ActionKey, cfg: Mapping, *, timeout: float | None = None) -> Verdict` builds the state/request, calls `client.ask`, verifies `result.model` is non-empty, and returns `evaluate(...)`; any `JudgeUnavailable` propagates.

- [ ] **Step 1:** Failing tests: serialized request for a key contains exactly the five key facts and nothing else (assert the JSON body keys; assert the `target` digest, a canary command string placed in a `RawAction.command`, hints and display never appear in the serialized body); `owner_context` rejects unknown keys and long values; thresholds boundary table (just below/at/above) for each question; `covers` low escalates; NaN cannot reach `evaluate` (parse already rejects) but `evaluate` also refuses non-finite; reasons are static; `consult` against an unreachable endpoint raises `JudgeUnavailable`; integration test for `consult` against the real System One mock.
- [ ] **Step 2:** Run; fail. **Step 3:** Implement. **Step 4:** Pass; ruff.
- [ ] **Step 5:** Commit `feat(assistant-hub): judge question set and escalation verdict`.

---

### Task 4: Shadow records and the async recorder

**Files:**
- Modify: `herdr_engineering/control_plane/assistant.py` (new collection `judge_records`, bounded 5,000 per tenant like requests/evidence, snapshot/restore with `snap.get(...)`, store methods `add_judge_record`, `get_judge_record_by_request(tenant_id, user_id, request_id)`, `update_judge_label(tenant_id, user_id, request_id, **fields)`, `list_judge_records(tenant_id, user_id, judge_version=None)`; all tenant+user scoped, defensive copies, same cap-trim-on-restore behavior as Task 4 of Phase 2)
- Create: `herdr_engineering/control_plane/assistant_hub/shadow.py`
- Test: `tests/control_plane/test_assistant_hub_shadow.py`

**Interfaces:**
- Produces dataclass `JudgeRecord(record_id, tenant_id, user_id, worker_id, session_id, task_id, request_id, key_id, verb, risk_class, judge_version, model, status: "ok"|"unavailable", reason_code, destructive, irreversible, severity, covers, escalate, created_at, owner_decision: ""|"approved"|"denied", decided_at, regret: bool, label_note)` (all defaulted; NO command text, display or canonical target).
- Produces `class ShadowRecorder`: `__init__(self, store, cfg_getter: Callable[[], Mapping], client_factory: Callable[[], JevClient | None], now: Callable[[], datetime], queue_size: int = 100)`; `start()`, `stop(timeout)`, `submit(job: ShadowJob) -> bool` (non-blocking; when the queue is full the OLDEST job is dropped and `dropped` counter increments; returns False if the recorder is stopped or the judge is disabled); a single daemon worker thread that calls `consult` OUTSIDE any store lock with `timeout=30`, writes a `JudgeRecord(status="ok")` or `status="unavailable", reason_code=<JudgeUnavailable.code>` and never raises. `ShadowJob` carries only ids, `ActionKey`, tenant/user/worker/session/task and request_id.
- Produces `label_decision(store, cfg_getter, tenant_id, user_id, request_id, approved: bool, now)` that sets `owner_decision`/`decided_at` on the matching record if present (ignored when absent); `mark_regret(store, tenant_id, user_id, key_id, worker_id, now)` sets `regret=True` on all records for the key (called when the owner revokes a grant).

- [ ] **Step 1:** Failing tests: store round trip through snapshot/restore and a real persist file; old snapshot without the key loads; per-tenant cap with oldest dropped; tenant+user scoping on every get/list/update (same-tenant other-user cases included); recorder with the judge DISABLED submits nothing and starts no thread; recorder with an unreachable endpoint writes `status="unavailable"` records and the worker survives; queue overflow drops oldest and counts; `stop()` joins the thread; two submits for the same request_id produce ONE record; no store lock held during `consult` (test by holding a probe that tries to take `store._lock` with a short timeout from another thread while a listener-based slow judge call is in flight); record contains no command text (canary).
- [ ] **Step 2:** Run; fail. **Step 3:** Implement. **Step 4:** Pass; ruff; rerun Phase 2 hub tests.
- [ ] **Step 5:** Commit `feat(assistant-hub): shadow judge records and async recorder`.

---

### Task 5: Calibration math, report and the gate

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/calibration.py`
- Modify: `herdr_engineering/control_plane/assistant_hub/actiongrants.py` (`effective_state` takes the current judge version; a grant whose `judge_version` is non-empty and differs resets to SHADOW)
- Test: `tests/control_plane/test_assistant_hub_calibration.py`

**Interfaces:**
- Produces `clopper_pearson_upper(failures: int, trials: int, confidence: float = 0.95) -> float` computed exactly with stdlib only (binomial CDF via `math.lgamma`/summation and bisection on p; `trials == 0` => 1.0; `failures == trials` => 1.0); verified against known values in tests (0/30 => 0.0950, 0/100 => 0.0295, 1/10 => 0.3942 within 1e-3, 5/200 => ~0.0527).
- Produces `@dataclass(frozen=True) class ClassReport: risk_class: str; labeled: int; judge_said_safe: int; false_negatives: int; fn_upper_bound: float; reliability: tuple[Bin, ...]` and `@dataclass(frozen=True) class CalibrationReport: judge_version: str; classes: Mapping[str, ClassReport]; generated_at: datetime; labels_note: str` (the note states plainly that labels are owner decisions, which are noisy).
- Ground truth (spec 13): a record is LABELED when status is ok, `owner_decision` is set, and `judge_version` equals the current version. "Judge said safe" = `escalate` is False. A FALSE NEGATIVE = judge said safe AND (`owner_decision == "denied"` OR `regret`). Owner approvals are NOT treated as proof of safety. Labels count once per `(worker, session, task, key_id)` so one session cannot inflate counts. Reliability bins: 10 equal-width bins on the destructive probability with (n, denied-or-regret rate).
- Produces `build_report(records, judge_cfg, now) -> CalibrationReport` and `gate_satisfied(report: CalibrationReport, judge_cfg: Mapping, risk_class: str) -> tuple[bool, str]` returning `(True, "")` only when `judge_cfg["enabled"] is True`, `judge_cfg["backend_confirmed"] is True`, `labeled >= min_labeled_decisions` for that class, and `fn_upper_bound < max_false_negative_bound`; otherwise `(False, <static reason code>)` with codes `judge_disabled`, `backend_unconfirmed`, `insufficient_labels`, `fn_bound_exceeded`.
- `ActionGrant.effective_state(now, policy_version, judge_version="")`: when `judge_version` is passed and the grant's recorded `judge_version` is non-empty and different, ACTIVE/PROPOSED => SHADOW. (Old calls without the argument keep working.)

- [ ] **Step 1:** Failing tests: Clopper-Pearson known values and monotonicity; labeled/false-negative counting for every ground-truth rule (approved+safe is not a FN; denied+safe is; regret+safe is; denied+escalate is not; unavailable records are ignored; old judge version ignored; duplicate `(worker,session,task,key)` counted once); per-risk-class separation; gate table (each failing code, and the one passing case); `effective_state` judge-version reset; report note present.
- [ ] **Step 2:** Run; fail. **Step 3:** Implement. **Step 4:** Pass; ruff; Phase 2 tests.
- [ ] **Step 5:** Commit `feat(assistant-hub): calibration report, false-negative bound and judge gate`.

---

### Task 6: Decision integration and the real activation gate

**Files:**
- Modify: `herdr_engineering/control_plane/assistant_hub/policy.py` (judge consultation in the authorize path; recorder submit on new pending rows; labeling in `record_owner_decision`; regret mark in `revoke_action_grant`; `activate_grant` computes the gate itself), `herdr_engineering/control_plane/assistant_hub/mcp.py` and `api.py` (pass the judge collaborators; replace the hard-coded `judge_gate_satisfied=False` in the activate route)
- Test: `tests/control_plane/test_assistant_hub_policy_judge.py`

**Interfaces:**
- Consumes: Tasks 1-5.
- Produces: a `JudgeRuntime` dataclass (in `judge.py`) `JudgeRuntime(cfg_getter, client_factory, recorder, now)` created once by the CLI wiring and attached to the hub/server like `assistant_workers`; `None` means "no judge": `decide` then treats an otherwise-authorizable ACTIVE grant as `pending` with `source="judge_unavailable"` (fail closed, spec 6.3 step 6), and never submits records.
- `decide(...)` gains an optional keyword `judge: JudgeRuntime | None = None`. In the authorize path only: consult the judge synchronously with `timeout = cfg.timeout_seconds`; `JudgeUnavailable` => `pending`, `source="judge_unavailable"`; `verdict.escalate` => `pending`, `source="judge"`, reason static; otherwise `authorized` and the audit entry includes `judge_version` and the integer-rounded percent of each question (no raw floats beyond 2 decimals). Every new `pending` row (not dedupe repeats) is also submitted to the recorder; vetoed requests are never sent to the judge.
- `record_owner_decision` calls `label_decision`; `revoke_action_grant` calls `mark_regret` for the grant's key/worker.
- `activate_grant(..., judge_gate_satisfied: bool)` is REPLACED by `judge: JudgeRuntime | None`: the function builds the report from the store and evaluates `gate_satisfied` for the grant's risk class; refusal codes: `judge_disabled`, `backend_unconfirmed`, `insufficient_labels`, `fn_bound_exceeded` (all 409), with the existing precedence (not_found, not_proposed, still_in_shadow, confirmation_missing, then the judge codes). The activate route passes the server's `JudgeRuntime`. Grants record `judge_version` on activation.

- [ ] **Step 1:** Failing tests: no judge configured => ACTIVE grant yields `pending/judge_unavailable` (never authorized); unreachable judge => same; escalating verdict => `pending/judge` (needs a real verdict: drive `evaluate` through a hand-built `JudgeResult` via the pure function by giving `decide` a `JudgeRuntime` whose `client_factory` returns a REAL `JevClient` against the real System One mock in the integration tier, and in the always-run tier construct the situation with a non-escalating/unavailable client against a closed port; for the escalate/non-escalate branches in the always-run tier call the decision helper `_apply_verdict(verdict)` directly - a pure function, not a mock); pending rows are submitted once and dedupe repeats do not resubmit; vetoed requests never reach the recorder; owner decision labels the record; revoke marks regret; activation gate refusals for each code with the exact precedence; activation succeeds ONLY when a populated store (200+ labeled records built through real `JudgeRecord`s) and `backend_confirmed: true` and the bound is met, and the resulting grant carries `judge_version`; judge-version change after activation => `effective_state` SHADOW and the next `decide` is `pending`; the activate route passes the runtime (route test with the real server) and answers `insufficient_labels`/`backend_unconfirmed` appropriately; Phase 2 tests updated (the helper that called `activate_grant(..., judge_gate_satisfied=True)` now builds a satisfied gate through real records or uses a small test-only constructor `force_active_grant_for_tests` located in `tests/` only, NOT in the package).
- [ ] **Step 2:** Run; fail. **Step 3:** Implement. **Step 4:** Pass; ruff; run all Phase 1 and Phase 2 hub tests, service/api tests.
- [ ] **Step 5:** Commit `feat(assistant-hub): consult the judge on the authorize path and wire the real activation gate`.

---

### Task 7: Key registration, status routes, `judge_status` tool

**Files:**
- Modify: `herdr_engineering/cli.py` (`assistant-judge` subcommand: `register`, `status`, `check`), `herdr_engineering/control_plane/api.py` (`GET /api/v1/assistant-judges` status, `POST /api/v1/assistant-judges/check`), `herdr_engineering/control_plane/assistant_hub/mcp.py` (`judge_status` tool, scope `read`, wired like the other read tools), CLI wiring in `cli._wire_assistant_hub` to build the `JudgeRuntime` and start/stop the recorder
- Test: `tests/control_plane/test_assistant_hub_judge_register.py`

**Interfaces:**
- `control-plane assistant-judge register --endpoint URL --key-file PATH --key-stdin [--model ID]`: reads the key from stdin only (refuses a TTY-less empty read; never from argv), writes it to `PATH` created with `O_EXCL|O_WRONLY` mode 0600 via a temp file in the same directory then `os.replace` (rotation: if `PATH` exists, the new key is written to `PATH.new`, health-checked with the new key, and only then replaces `PATH`; on failure `PATH` is untouched and `PATH.new` removed), performs `GET /v1/models` with the new key, verifies the configured/`--model` id is present, prints ONLY `registered: fingerprint=<8hex> model=<id> models_seen=<n>` and a reminder line to add `api_key_file`/`endpoint`/`model` to the YAML and to set `backend_confirmed` only after verifying a real model is serving. It does NOT edit the YAML. Exit codes 0/1/2; errors are one line with the JudgeUnavailable code, never the response body.
- `control-plane assistant-judge status`: calls `GET /api/v1/assistant-judges` (operator key via `--api-key-file`/`--control-plane`) and prints enabled, model, judge_version, backend_confirmed, per-class labeled/FN/bound, gate result per class. `check` calls the POST check route, which runs one synthetic `consult` (a fixed harmless key: verb `shell.read_only`) with the configured timeout and reports `ok`/code and latency; it records NO JudgeRecord and does not touch grants.
- `GET /api/v1/assistant-judges` (operator only; same-tenant/user scoping from `_assistant_scope()`): `{enabled, model, judge_version, backend_confirmed, endpoint_host, key_fingerprint, recorder: {queue, dropped}, calibration: <CalibrationReport as dict>, gate: {risk_class: {satisfied, code}}}`; the endpoint is shown as host only; the key file path is NOT shown; no key material. Wired into the same five places as the Phase 2 routes (GET dispatch, POST dispatch, origin/415 set, mutable-resources set, mutation tuple).
- MCP `judge_status` (scope `read`): `{enabled, mode: "shadow", judge_version, labeled_by_class, gate: {risk_class: bool}}` only - no probabilities of individual requests, untrusted banner not needed (hub-generated) but follows the `_text` helper.
- The key fingerprint is computed from the key file on demand (sha256 hex[:8]) and never cached in the store.

- [ ] **Step 1:** Failing tests: CLI register with a REAL key via stdin into a tmp file in a throwaway HOME: file mode 0600, fingerprint printed, key never in stdout/stderr/exception text/`ps`-visible argv (assert the argv of the subprocess contains no key); register against a closed port fails with exit 1, leaves no key file and no `.new`; rotation keeps the old file when the new key fails; refuses a key file path that is a symlink or in a world-writable directory; status/check routes operator-only (viewer 403, worker credential 401/403, no key 401), same-tenant-other-user isolation, cross-tenant 404/empty; `judge_status` tool scope-gated and exposes no per-request data; response bodies contain no key, no file path; CLI `status` against a real server; integration tier: register against the real System One mock.
- [ ] **Step 2:** Run; fail. **Step 3:** Implement. **Step 4:** Pass; ruff; all hub/api/cli tests.
- [ ] **Step 5:** Commit `feat(assistant-hub): judge key registration, status routes and judge_status tool`.

---

### Task 8: End-to-end, docs, config example

**Files:**
- Create: `tests/control_plane/test_assistant_hub_phase3_e2e.py`
- Modify: `docs/operations/assistant-hub.md`, `deploy/control-plane/fleet-config.example.yaml`

- [ ] **Step 1:** E2E (real `ControlPlaneServer` + `HubServer`, judge configured against a closed loopback port in the always-run tier): action requests become `pending`; shadow records appear with `status="unavailable"` and the owner status route shows `insufficient_labels`/`backend_unconfirmed`; owner approvals label records (hand-written `JudgeRecord`s with `status="ok"` populate the store to reach the label threshold, labeled "hand-built records stand in for a real judge"); with `backend_confirmed` false the activate route answers `backend_unconfirmed`; flipping the flag and meeting the bound makes activate succeed and the exact-key request `authorized` only when the judge answers (integration tier against the real System One mock); judge unreachable at authorize time => `pending/judge_unavailable`; escalate => `pending/judge`; change `model` in config => grant SHADOW; `verify_audit` passes; no key, key path, or command text in audit/records/snapshot/MCP output.
- [ ] **Step 2:** Docs: Phase 3 section in `assistant-hub.md`: how to register a key, the YAML block, `backend_confirmed` semantics and why the response cannot prove a real model (mock looks identical; use `GET /v1/systemone/runtimes` or the operator's knowledge), what is sent to the judge and what never is, fail-closed table, shadow records, ground-truth rules, the gate and its refusal codes, judge version resets, the recorder (async, bounded, drop-oldest), what is deferred (canary, hook-attested evidence and in-session confirmation in Phase 4, Codex/OpenCode/Pi adapters), the known unverified items (production serves Laya vs mock; model ids on the Go gateway; real-Laya determinism), and the integration-test environment variables. Commented `judge:` block in `fleet-config.example.yaml` with real keys/defaults.
- [ ] **Step 3:** Run all hub/service/api/central-config/cli tests; ruff on touched files.
- [ ] **Step 4:** Commit `test+docs(assistant-hub): phase 3 end-to-end and operator docs`.

---

## Deferred / owner actions

- **Owner action required before any autonomy:** register a Jev key, confirm which backend serves production (`GET /v1/models` + `GET /v1/systemone/runtimes`, or ask whoever runs the worker), set `backend_confirmed: true`, then run the shadow period and let labels accumulate. The gate cannot pass before 200 labeled decisions per risk class.
- Phase 4: dynamic activation UX, in-session confirmation, hook-attested evidence, owner digest, regret command, canary.
- Harness adapters (Codex/OpenCode/Pi), email/spec ingestion, dispatcher.

## Self-review

- Spec coverage: 7.1 -> Task 1; 7.2 -> Task 7 (CLI + status route; registering the key over HTTP is intentionally NOT built: secrets never travel in request bodies); 7.3 -> Tasks 2-3; 7.4 -> Tasks 4-5; 6.3 steps 3/4/6 -> Task 6; 5.3 `judge_status` -> Task 7; 13 ground truth -> Task 5. Differences from the spec are listed under "Facts established by the survey" and in the Global Constraints (real request contract, `backend_confirmed`, no HTTP key registration, canary deferred).
- Type consistency: `JudgeResult`, `JudgeUnavailable(code)`, `Verdict`, `JudgeRecord`, `JudgeRuntime`, `CalibrationReport`, `ClassReport` names are used identically across tasks.
- Proportion: 8 tasks; signatures and tests only.
