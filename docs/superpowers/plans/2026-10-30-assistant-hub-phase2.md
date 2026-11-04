# Assistant Hub Phase 2 Implementation Plan (verbs, canonical actions, vetoes, verb-level grants)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the hub a policy core: a verb catalog, a canonical `ActionKey` derived by the hub from raw actions (including shell commands), fixed vetoes, verb-level grant records with states, and an owner-answered `action_request` tool. Nothing becomes autonomous in this phase.

**Architecture:** Pure, stateless policy modules in `assistant_hub/` (`verbs`, `shellparse`, `canonical`, `vetoes`, `policy`) over three new `AssistantStore` collections (`action_grants`, `action_requests`, `action_evidence`) mirroring how `worker_events` was added. The MCP `action_request` tool runs the spec 6.3 decision order; owner routes decide requests and manage grants. Activation of a grant is implemented but its judge gate is always unsatisfied until Phase 3, so no code path returns `authorized` from a learned grant in production.

**Tech Stack:** Python 3.12, stdlib only (`shlex` plus a hand-written operator/substitution scanner; bashlex and tree-sitter are not installed and no dependency is added), existing `AssistantStore`/`Hub`/`ControlPlaneServer`, pytest with real objects and real HTTP (no mocks).

**Spec:** `docs/superpowers/specs/2026-10-30-assistant-claude-mcp-and-jev-design.md` sections 4, 5.3, 5.4, 6, 8, 13, 15, 16. Phase 1 plan/PR: `docs/superpowers/plans/2026-10-30-assistant-hub-phase1.md`, PR 537.

## Global Constraints

- Branch `feat/assistant-hub-phase2` off origin/main; never commit to main; plain `git commit`/`git push` (no rebase, no force-push); no merge without the owner's word. Commit trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- TDD, no mocks/stubs/fakes (real stores, real servers on port 0, real subprocess for CLI); tests never touch real `~/.claude`, `~/.codex`, `~/.pi`, `~/.config/herdr`, `~/.config/opencode` (throwaway HOME).
- `/api/v1` tests need `asyncpg`; sandbox runs use `PYTHONPATH=/tmp/claude-1000/-home-wohlgemuth-IdeaProjects/a8939f5b-bb45-4f5f-a41d-65023acf9586/scratchpad/deps`.
- Strings are never matched: grants match only on exact canonical-key equality (spec 6.1). No wildcards in a learned grant.
- The judge (Phase 3) does not exist here. Decision order steps 4 and 6 collapse to "no judge available => fail closed => owner approval". Judge can only add friction, never remove a requirement.
- Learned state lives only in the hub store; harness code stays a stateless relay (spec 16.1). The Phase 1 hook relay is NOT changed in this phase and still returns no decision.
- Worker credentials can never decide a request, confirm/activate a grant, or be listed in `actions.approvers` (spec 5.4, 8). Owner routes require the operator API key.
- Evidence from workers (`report`, MCP-originated) is `worker_reported` and counts for nothing. Only owner-API decisions (`owner_api`) count toward N. `harness_attested` counting is deferred to Phase 4 because it needs hook changes.
- Defaults (spec 6.2, builder's calls): shadow period 14 days; N by risk class local=5, remote=10, outward=30; distinct sessions >= 3 and distinct tasks >= 2; new (worker, repo) scope for a confirmed verb needs max(3, N//3) approvals; grant expiry 30 days.
- Unknown verb, unparseable shell, or any element mapping to an unknown/mutating verb => owner decides (never an auto result).
- Fixed vetoes (spec 4) are evaluated first, hard-coded, extendable but never reducible by config.
- Port/config style: new config section `control_plane.personal_assistant.grants` validated like `_hub_config` (unknown keys raise, real bools/ints only).

## Review Focus

Failure modes the spec implies that no single task owns; each has a pinning test in the task named.

1. Shell evasion of verb mapping: `git push --force-with-lease`, `git push origin +main`, `git push origin :main`, `FOO=1 git push -f`, `git -c x=y push --force`, `sh -c 'git push -f'`, `bash -lc "..."`, `$(...)`/backtick substitutions, newlines, `&&`/`;`/`||`/pipes, `xargs git push`, env/`command`/`exec`/`time`/`nohup` wrappers, here-strings. Each must map to the stricter verb or to unknown => owner (Task 2, 3).
2. Quobyte/Fry/secret path tricks: `rm -rf /quobyte/..`, relative paths and `..` that resolve into `/quobyte`, symlinks resolved only when the hub can; worker hint paths are `worker_attested` and the stricter of hint and derivation wins (Task 3).
3. Evidence farming: one session repeating a request, one task, one worker enumerating many sessions with the same session id, evidence for a different key/scope, worker_reported evidence (Task 5).
4. Grant creep: a grant for `(worker A, repo X, verb V)` must not authorize repo Y, worker B, another verb, a different flag class, or a case/whitespace/unicode variant of the target (Task 6).
5. Expiry/revocation/policy-version reset take effect on the very next `action_request`, including across store persist/restore (Task 4, 6).

---

### Task 1: Verb catalog and ActionKey

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/verbs.py`
- Test: `tests/control_plane/test_assistant_hub_verbs.py`

**Interfaces:**
- Produces: `class RiskClass(enum.StrEnum)`: `LOCAL_REVERSIBLE="local_reversible"`, `REMOTE_REVERSIBLE="remote_reversible"`, `OUTWARD="outward"`.
- Produces: `class Verb(enum.StrEnum)` with at least `email.draft, email.send, pr.comment, pr.merge, deploy.canary, deploy.prod, git.push, git.force_push, artifact.create, shell.read_only, shell.mutating, cluster.read_only, cluster.mutating, fs.delete, unknown`; `VERB_RISK: dict[Verb, RiskClass]` (draft/comment/push/artifact/read-only = local or remote reversible, send/merge/deploy/force_push/cluster.mutating/fs.delete/shell.mutating/unknown = OUTWARD).
- Produces: `@dataclass(frozen=True) class ActionKey: tool: str; verb: Verb; resource_scope: str; target: str; flag_class: FlagClass; risk_class: RiskClass` with `canonical() -> str` (stable JSON, sorted keys, NFKC-normalized, lowercase `tool`/`resource_scope`, no whitespace) and `key_id() -> str` (sha256 hex of `canonical()`); `class FlagClass(enum.StrEnum)`: `NONE, FORCE, HARD, RECURSIVE, NO_VERIFY, MULTIPLE`.
- Produces: `STRICTER(a: ActionKey, b: ActionKey) -> ActionKey` choosing the higher `risk_class`, then the more severe `flag_class`; ties keep `a`.

- [ ] **Step 1:** Write failing tests: `test_canonical_is_stable_across_field_order_and_unicode_variants` (NFKC `ａ` equals `a`), `test_key_id_differs_for_every_field`, `test_every_verb_has_a_risk_class`, `test_unknown_verb_is_outward`, `test_stricter_prefers_higher_risk_then_flag`, `test_action_key_is_immutable`.
- [ ] **Step 2:** Run `python3 -m pytest tests/control_plane/test_assistant_hub_verbs.py -q`; expect import failure.
- [ ] **Step 3:** Implement `verbs.py` per Interfaces.
- [ ] **Step 4:** Run the tests; expect pass. `ruff check` the two files.
- [ ] **Step 5:** Commit `feat(assistant-hub): verb catalog and canonical ActionKey`.

---

### Task 2: Shell parser (conservative)

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/shellparse.py`
- Test: `tests/control_plane/test_assistant_hub_shellparse.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class SimpleCommand: argv: tuple[str, ...]; env: tuple[tuple[str, str], ...]; wrapped: bool` (`wrapped` = came from inside `sh -c`/`bash -c`/substitution).
- Produces: `class Unparseable(Exception)`.
- Produces: `parse(command: str) -> list[SimpleCommand]`, flattening `&&`, `||`, `;`, `&`, newlines, pipes, subshell parentheses, `$(...)`, backticks, `sh|bash|zsh|dash -c <str>` (also `-lc`, `-ic`) recursively to depth 4, and stripping leading `VAR=val`, `env`, `command`, `exec`, `time`, `nohup`, `nice`, `sudo` wrappers (keeping the wrapped argv; `sudo` marks the command via `env` entry `("__sudo__","1")`). `xargs <cmd>` yields `<cmd>` with an appended unknown marker argv element `"__xargs__"`. Raises `Unparseable` on unbalanced quotes/parentheses, depth > 4, NUL bytes, input > 8 KiB, unsupported heredoc/process-substitution/`eval`, or any construct the scanner cannot classify.

- [ ] **Step 1:** Failing tests (each asserts exact argv lists): `test_splits_and_or_semicolon_newline_background`, `test_pipe_yields_every_stage`, `test_env_assignment_and_wrappers_are_stripped`, `test_sh_dash_c_is_recursed`, `test_command_substitution_and_backticks_are_extracted`, `test_xargs_marks_unknown_tail`, `test_quotes_do_not_split`, and parametrized `test_unparseable_raises` for: unbalanced quote, unbalanced paren, `eval`, heredoc `<<EOF`, process substitution `<(...)`, 9 KiB input, NUL, nesting depth 5.
- [ ] **Step 2:** Run; expect failure.
- [ ] **Step 3:** Implement `parse` with a single left-to-right scanner honoring single/double quotes and backslashes, using `shlex.split(posix=True)` only on already-isolated simple-command text. No `eval`/`exec` of input, no subprocess.
- [ ] **Step 4:** Run tests; pass; ruff.
- [ ] **Step 5:** Commit `feat(assistant-hub): conservative shell parser for action canonicalization`.

---

### Task 3: Canonicalizer (raw action -> ActionKey) and fixed vetoes

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/canonical.py`, `herdr_engineering/control_plane/assistant_hub/vetoes.py`
- Test: `tests/control_plane/test_assistant_hub_canonical.py`, `tests/control_plane/test_assistant_hub_vetoes.py`

**Interfaces:**
- Consumes: `parse`, `Unparseable`, `SimpleCommand` (Task 2); `ActionKey`, `Verb`, `FlagClass`, `STRICTER`, `VERB_RISK` (Task 1).
- Produces (canonical.py): `@dataclass(frozen=True) class RawAction: tool: str; command: str | None = None; operation: str | None = None; recipients: tuple[str, ...] = (); repo: str | None = None; environment: str | None = None; hint: Mapping[str, str] = field(default_factory=dict)`; `@dataclass(frozen=True) class Derived: key: ActionKey | None; elements: tuple[ActionKey, ...]; owner_decides: bool; reason: str; worker_attested_facts: tuple[str, ...]`; `derive(raw: RawAction) -> Derived`. A shell command yields one `ActionKey` per simple command in `elements`; `key` is the stricter of all; any `Unparseable`, unknown verb or unknown command family sets `owner_decides=True` (key may be the `unknown` verb). Families: `git` (push -> `git.push`; `-f`, `--force`, `--force-with-lease`, `+refspec`, `:ref` delete, `--mirror` -> `git.force_push` with FlagClass.FORCE; reset --hard -> `shell.mutating` FlagClass.HARD; `-c`/`-C` global options skipped before the subcommand), `gh` (pr comment -> `pr.comment`; pr merge -> `pr.merge`; api with non-GET -> `shell.mutating`; release/workflow -> unknown), `ssh|srun|sbatch|scancel|squeue|sacct|scontrol` (read-only subcommands -> `cluster.read_only`, others -> `cluster.mutating`; ssh remote command is parsed recursively), `rm|mv|cp|chmod|chown|dd|truncate` (mutating; `rm` -> `fs.delete`, `-r`/`-R`/`-rf` -> RECURSIVE), read-only utilities (`ls cat head tail grep rg find(no -delete/-exec) stat wc diff git status|log|diff|show|branch(no -d)`) -> `shell.read_only`, structured `email` (`operation` draft -> `email.draft`, send -> `email.send`, scope = lowercased recipients sorted, recipients >1 -> FlagClass.MULTIPLE), `deploy` (canary vs prod from `environment`; anything else unknown). `resource_scope`: `org/name` repo from `raw.repo` or git remote argv, else `""`. Path targets are resolved with `os.path.normpath` against a `cwd` hint; the hub does not follow remote symlinks, so any path fact from `hint` is listed in `worker_attested_facts` and, if hint and derived disagree, `STRICTER` wins.
- Produces (vetoes.py): `@dataclass(frozen=True) class Veto: code: str; reason: str`; `FIXED_VETOES: tuple[...]`; `check_vetoes(derived: Derived, extra: Sequence[VetoRule] = ()) -> Veto | None` evaluated over every element, never the stricter alone. Seeded vetoes: `git.force_push` anywhere; commands touching a path under `/quobyte` with `fs.delete` or `shell.mutating`; any argv/host containing `fry.fiehnlab.ucdavis.edu`; cluster no-touch: `cluster.mutating` whose argv installs (`pip|apt|apt-get|dnf|yum|conda|mamba|cargo install|npm -g|crontab|systemctl|apptainer build`) or edits services on a node; branch-protection disable (`gh api` on `/branches/*/protection` with DELETE/PUT/PATCH); secret printing (`cat|echo|printenv|env` of paths/vars matching `*key*|*token*|*secret*|*.pem|id_rsa*|.env`). `extra` rules can only add vetoes; passing a rule list never removes a fixed one (test).

- [ ] **Step 1:** Failing tests (canonical): parametrized table of >= 40 `(raw, expected verb, flag_class, owner_decides)` rows covering every Review Focus item 1 form, benign reads (`git status && ls`), `echo hi | git push -f` (stricter wins), `email` draft vs send with 2 recipients, deploy canary vs prod, hint path disagreement keeps the stricter and records the fact, unparseable => `owner_decides`.
- [ ] **Step 2:** Failing tests (vetoes): one test per seeded veto that tries to reach it through two wrapper/evasion forms each, `test_extra_rules_never_reduce_fixed_vetoes`, `test_veto_checks_every_element_not_just_the_strictest`, `test_secret_name_variants` (case, `.ENV`, `~/.ssh/id_ed25519`), `test_quobyte_relative_dotdot_resolves_into_quobyte`, `test_relative_path_outside_quobyte_is_not_vetoed`.
- [ ] **Step 3:** Run; expect failure.
- [ ] **Step 4:** Implement `canonical.py` and `vetoes.py`. Keep each family a small function table; no regex that matches a whole command string.
- [ ] **Step 5:** Run both test files; pass; ruff.
- [ ] **Step 6:** Commit `feat(assistant-hub): canonical action derivation and fixed vetoes`.

---

### Task 4: `grants:` config and store collections

**Files:**
- Modify: `herdr_engineering/control_plane/assistant.py` (config: add `_grants_config` next to `_hub_config` and `"grants"` in `assistant_config`'s return; store: three collections)
- Create: `herdr_engineering/control_plane/assistant_hub/actiongrants.py` (dataclasses and state machine)
- Test: `tests/control_plane/test_assistant_hub_grants_config.py`, `tests/control_plane/test_assistant_hub_actiongrants.py`

**Interfaces:**
- Produces config: `control_plane.personal_assistant.grants` keys and defaults: `shadow_days: 14`, `shadow_started_at: null` (ISO datetime; hub sets on first start if null and persists), `required_approvals: {local_reversible: 5, remote_reversible: 10, outward: 30}`, `min_sessions: 3`, `min_tasks: 2`, `grant_ttl_days: 30`, `extra_vetoes: []` (list of `{code, reason, verb?, argv_contains?}` mappings), `policy_version: 1`. Unknown keys raise `ValueError("control_plane.personal_assistant.grants has unknown keys: [...]")`; ints must be real ints >= 1; `policy_version` int >= 1.
- Produces dataclasses in `actiongrants.py` (all fields with defaults for old-snapshot restore): `ActionGrant(grant_id, tenant_id, user_id, worker_id, resource_scope, key_id, verb, key_canonical, state: GrantState, created_at, proposed_at, activated_at, expires_at, revoked_at, policy_version, judge_version="", confirmed_by="")`; `GrantState(StrEnum)`: `SHADOW, PROPOSED, ACTIVE, EXPIRED, REVOKED`; `ActionRequestRow(request_id, tenant_id, user_id, worker_id, session_id, task_id, key_canonical, key_id, verb, risk_class, status: pending|approved|denied|vetoed, reason, created_at, decided_at, decided_by)`; `ActionEvidence(evidence_id, tenant_id, user_id, worker_id, session_id, task_id, key_id, outcome: approved|denied, provenance: owner_api|worker_reported, created_at)`.
- Produces store methods on `AssistantStore` (each takes `self._lock`, then `_after_mutation()`; snapshot/restore include the new collections and old snapshots without them still load): `put_action_grant`, `get_action_grant`, `list_action_grants(tenant_id, user_id)`, `put_action_request`, `get_action_request`, `list_action_requests(tenant_id, user_id, status=None)`, `add_action_evidence`, `list_action_evidence(tenant_id, user_id, key_id=None)`. Evidence and requests are bounded: 5,000 rows per tenant each, oldest dropped (count indexes rebuilt on restore as for `worker_events`).
- Produces state functions: `ActionGrant.effective_state(now, policy_version) -> GrantState` (ACTIVE past `expires_at` => EXPIRED; ACTIVE/PROPOSED with older `policy_version` than current => SHADOW per spec 6.2), `revoke(grant, now)`.

- [ ] **Step 1:** Failing config tests mirroring `test_assistant_hub_config.py`: defaults, overrides, unknown key, non-mapping, bool-as-int rejected, zero rejected, bad `extra_vetoes` entry rejected.
- [ ] **Step 2:** Failing store tests: round-trip through `snapshot()`/`restore()` and through a real persist file; an old snapshot (no new keys) restores; per-tenant cap drops the oldest; `test_policy_version_bump_resets_active_grant_to_shadow`; `test_expiry_is_effective_without_a_sweeper` (state computed from `now`); persist file mode stays 0600.
- [ ] **Step 3:** Run; fail.
- [ ] **Step 4:** Implement config validator and collections following the `worker_events` pattern (survey: `__init__`, `snapshot()`, `restore()`, `_e_to_dict`/`_e_from_dict`).
- [ ] **Step 5:** Pass; ruff. Run `tests/control_plane/test_assistant_service.py` and `test_assistant_hub_*.py` to confirm no regression.
- [ ] **Step 6:** Commit `feat(assistant-hub): grants config section and action grant/request/evidence storage`.

---

### Task 5: Evidence counting and grant proposals

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/evidence.py`
- Test: `tests/control_plane/test_assistant_hub_evidence.py`

**Interfaces:**
- Consumes: `ActionEvidence`, `ActionGrant`, config `grants` dict, `RiskClass` (Tasks 1, 4).
- Produces: `count_qualifying(evidence: Sequence[ActionEvidence], key_id: str, worker_id: str, resource_scope: str) -> Counts` where `Counts(approvals: int, sessions: int, tasks: int, denials: int)`: counts only `outcome="approved"` and `provenance="owner_api"` for the exact key; each distinct `(session_id)` and `(task_id)` counted once; any denial for the key within the counted window resets approvals to the count after the latest denial (a denial restarts the streak).
- Produces: `required_approvals(risk: RiskClass, cfg: Mapping, scope_is_new_for_confirmed_verb: bool) -> int` (cfg table; new scope on a confirmed verb => `max(3, N // 3)`).
- Produces: `propose_if_ready(store, cfg, now, tenant_id, user_id, worker_id, key: ActionKey) -> ActionGrant | None`: creates/updates a `PROPOSED` grant only when `approvals >= required`, `sessions >= min_sessions`, `tasks >= min_tasks` and the global shadow period has elapsed; never creates `ACTIVE`; idempotent (a second call returns the same grant).

- [ ] **Step 1:** Failing tests: `test_one_session_repeating_does_not_count_past_one_session`, `test_requires_distinct_sessions_and_tasks`, `test_worker_reported_evidence_counts_nothing`, `test_denial_restarts_the_streak`, `test_evidence_for_a_different_key_scope_or_worker_is_ignored`, `test_threshold_by_risk_class`, `test_new_scope_for_confirmed_verb_uses_third_rule`, `test_no_proposal_during_global_shadow`, `test_propose_is_idempotent_and_never_active`.
- [ ] **Step 2:** Run; fail. **Step 3:** Implement. **Step 4:** Pass; ruff.
- [ ] **Step 5:** Commit `feat(assistant-hub): evidence counting and grant proposals`.

---

### Task 6: Decision engine and activation gates

**Files:**
- Create: `herdr_engineering/control_plane/assistant_hub/policy.py`
- Test: `tests/control_plane/test_assistant_hub_policy.py`

**Interfaces:**
- Consumes: `derive`, `check_vetoes`, `ActionGrant`, store methods, `propose_if_ready`.
- Produces: `@dataclass(frozen=True) class Decision: outcome: Literal["authorized","denied","pending"]; reason: str; request_id: str | None; grant_id: str | None; veto_code: str | None; source: Literal["veto","grant","owner","shadow","unknown"]`.
- Produces: `decide(hub_store, cfg, worker, raw: RawAction, session_id: str, task_id: str, now) -> Decision` implementing spec 6.3: (1) vetoes over every element => `denied`, row status `vetoed`, audit `assistant.action.vetoed`; (2) worker scope check is done by the caller; (3) an `ACTIVE` (effective state) grant whose `worker_id`, `resource_scope`, `key_id` all equal the derived key AND no element is `owner_decides` => `authorized`, audit with grant id; (4) judge: none exists, so skipped; (5)/(6) otherwise `pending` with a stored `ActionRequestRow`. During the global shadow period (`shadow_started_at + shadow_days > now`) even an ACTIVE grant yields `pending` with `source="shadow"`. `owner_decides` always yields `pending`.
- Produces: `record_owner_decision(store, cfg, now, tenant_id, user_id, request_id, approve: bool, decided_by: str) -> ActionRequestRow` (idempotent: re-deciding a decided request raises `ValueError`; writes `ActionEvidence(provenance="owner_api")`, then calls `propose_if_ready`; audit `assistant.action.decided`).
- Produces: `activate_grant(store, cfg, now, tenant_id, user_id, grant_id, confirmed_by: str, judge_gate_satisfied: bool) -> ActionGrant`: raises `ActivationRefused("judge gate not satisfied")` unless every gate (grant `PROPOSED`, shadow elapsed, first-grant confirmation present as `confirmed_by`, `judge_gate_satisfied=True`) holds. Production callers pass `judge_gate_satisfied=False` until Phase 3, so activation cannot happen through any route in this phase; tests use `True` to prove the decision logic honors an active grant.

- [ ] **Step 1:** Failing tests: `test_veto_denies_even_with_active_grant`, `test_force_push_hidden_in_a_chain_is_denied`, `test_active_grant_authorizes_exact_key_only` (parametrized variants: other repo, other worker, other verb, flag-class change, case/whitespace/unicode target variants all `pending`), `test_unknown_or_unparseable_is_pending`, `test_shadow_period_blocks_authorization`, `test_expired_and_revoked_grants_do_not_authorize_on_next_call`, `test_policy_version_bump_blocks_old_grant`, `test_activate_refused_without_judge_gate`, `test_activate_requires_first_grant_confirmation`, `test_owner_decision_is_idempotent_and_records_owner_api_evidence`, `test_ready_evidence_creates_proposed_not_active`, `test_audit_chain_verifies_after_a_full_cycle` (use `verify_audit`).
- [ ] **Step 2:** Run; fail. **Step 3:** Implement `policy.py` (small functions; no I/O besides the store). **Step 4:** Pass; ruff.
- [ ] **Step 5:** Commit `feat(assistant-hub): decision engine, owner decisions and activation gates`.

---

### Task 7: MCP `action_request` and `action_grants_list`; scope opt-in

**Files:**
- Modify: `herdr_engineering/control_plane/assistant_hub/mcp.py` (`_TOOLS`, `_HANDLERS`), `events.py` (add kind `action_request`), `workers.py` (`DEFAULT_SCOPES = ("ask","report")`, new scope names `read` and `act`, `WorkerRegistry.set_scopes(tenant_id, user_id, worker_id, scopes)`, scope vocabulary validation)
- Test: update `tests/control_plane/test_assistant_hub_mcp.py` (exact tool-name set), new `tests/control_plane/test_assistant_hub_action_request.py`, `tests/control_plane/test_assistant_hub_workers.py`

**Interfaces:**
- Consumes: `decide`, `RawAction`, config (Tasks 3, 4, 6).
- Produces tool `action_request` (scope `act`): args `tool` (str), `command` (str, optional), `operation` (optional), `recipients` (list[str], optional), `repo`, `environment`, `hint` (object of str->str, <= 16 keys, <= 512 chars each), `session_id` (required), `task_id` (required); unknown keys rejected by `_check_args`. Returns `{"outcome": ..., "reason": ..., "request_id": ..., "grant_id": ..., "veto_code": ...}` as a decision with a reason (never an instruction). Shares the per-worker event rate limiter (30/min) and records an `action_request` event (payload: key canonical, outcome; never the raw command text beyond the 4 KiB scrubbed cap).
- Produces tool `action_grants_list` (scope `read`): the worker's own grants for the tenant/user, state computed via `effective_state`, wrapped with the untrusted banner like the other read tools.
- Produces: `WorkerRegistry.set_scopes(...)` validating each scope in `{"ask","report","read","act"}` (rejecting unknown names and the empty set), audited `assistant.worker.scopes_changed`. Existing workers keep their stored scopes; only newly redeemed workers get the narrower default.
- Produces: `redeem`/`create_enrollment` accept an optional `scopes` list so the owner can enroll with `read`/`act` up front.

- [ ] **Step 1:** Failing tests: tool-list exact set updated; `test_action_request_requires_act_scope`; `test_action_request_veto_returns_denied_with_code`; `test_action_request_unknown_is_pending_and_creates_row`; `test_action_request_cannot_set_tenant_or_user_via_args`; `test_action_request_rate_limited_at_31st`; `test_hint_over_cap_rejected`; `test_default_scopes_exclude_read` and `test_existing_worker_keeps_read`; `test_set_scopes_rejects_unknown_scope_and_empty`; `test_set_scopes_is_tenant_scoped`; `test_action_grants_list_only_own_tenant_and_user`; generic-exception branch still returns `{"error":"tool failed"}`.
- [ ] **Step 2:** Run; fail. **Step 3:** Implement. **Step 4:** Pass; ruff; rerun all Phase 1 hub tests.
- [ ] **Step 5:** Commit `feat(assistant-hub): action_request tool, grant listing and opt-in worker scopes`.

---

### Task 8: Owner routes and CLI

**Files:**
- Modify: `herdr_engineering/control_plane/api.py` (resources `assistant-action-requests`, `assistant-grants`, and `PATCH`-style scope change under `assistant-workers/<id>/scopes`, wired in the same five places as `assistant-workers`: GET dispatch ~3731, POST/DELETE dispatch ~4197, origin-check set ~2351, `_ASSISTANT_MUTABLE_RESOURCES` ~2452, mutation tuple ~2487), `herdr_engineering/cli.py` (`assistant-grant` and `assistant-action` subcommands plus `assistant-worker scopes` action and `--scopes` on enroll)
- Test: `tests/control_plane/test_assistant_hub_phase2_owner_routes.py` (real HTTP, reuse the `world` fixture pattern from `test_assistant_hub_owner_routes.py`)

**Interfaces:**
- Produces routes (all `self._require("operator")`; tenant/user from `_assistant_scope()`; worker credentials get 401/403):
  - `GET /api/v1/assistant-action-requests?status=pending` list; `POST /api/v1/assistant-action-requests/<id>/decision` body `{"approve": bool}` -> `record_owner_decision`.
  - `GET /api/v1/assistant-grants` list with effective state; `DELETE /api/v1/assistant-grants/<id>` revoke; `POST /api/v1/assistant-grants/<id>/activate` body `{"confirm": true}` calls `activate_grant(..., judge_gate_satisfied=False)` so it returns 409 `judge_gate_not_satisfied` in this phase (the route and refusal are tested; activation is reachable only after Phase 3 flips the gate).
  - `POST /api/v1/assistant-workers/<id>/scopes` body `{"scopes": [...]}` -> `set_scopes`.
- Produces CLI: `control-plane assistant-action list|approve|deny <id>`, `assistant-grant list|revoke|activate <id>`, `assistant-worker scopes <worker_id> --scopes a,b`, `assistant-worker enroll --scopes a,b`. Same urllib/`X-API-Key` pattern as `_cmd_cp_assistant_worker`.

- [ ] **Step 1:** Failing tests: operator key works, viewer key 403, worker credential 401/403 on every new route (this also pins the owner-vs-worker split the Phase 1 e2e did not cover); decision twice => 409; decision of another tenant's request => 404; activate => 409 with the exact code; revoke takes effect on the next MCP `action_request`; scope change visible to the next MCP call; CLI end-to-end via real subprocess in throwaway HOME for approve, revoke, scopes.
- [ ] **Step 2:** Run; fail. **Step 3:** Implement. **Step 4:** Pass; ruff; full hub test set.
- [ ] **Step 5:** Commit `feat(assistant-hub): owner routes and CLI for action requests, grants and worker scopes`.

---

### Task 9: End-to-end, docs, config example

**Files:**
- Create: `tests/control_plane/test_assistant_hub_phase2_e2e.py`
- Modify: `docs/operations/assistant-hub.md`, `deploy/control-plane/fleet-config.example.yaml` (commented `grants:` block with real keys/defaults)

- [ ] **Step 1:** E2E test with a real `ControlPlaneServer` + `HubServer`: enroll a worker with `act` scope; `action_request` of a force-push chain => `denied` (veto code present, audit entry); an unknown command => `pending`; owner approves via route; repeat from three sessions/two tasks (distinct ids) until the proposal appears in `GET /assistant-grants` as `proposed`; `activate` => 409; revoke a hand-inserted active grant (real store object) and prove the very next `action_request` is `pending`; `verify_audit` passes; neither the raw credential nor enrollment code appears in the audit or snapshot; worker_reported `report` evidence never advances the count.
- [ ] **Step 2:** Docs: add to `assistant-hub.md` sections on verbs and canonical actions, the veto list, grant states and defaults, why nothing is autonomous yet (Phase 3 judge gate, Phase 4 hook-attested evidence and in-session confirmation), the new `act`/`read` scopes and opt-in, the owner route/CLI table, and the hint trust rule. Add the commented config block.
- [ ] **Step 3:** Run the full hub + assistant service/api tests and `tests/test_central_config_servers.py`; ruff on touched files.
- [ ] **Step 4:** Commit `test+docs(assistant-hub): phase 2 end-to-end and operator docs`.

---

## Deferred to later phases (not in this plan)

- Phase 3: Jev client, `judge` config, key registration, shadow recorder, calibration gate (sets `judge_gate_satisfied`).
- Phase 4: hook-attested evidence counting, in-session first-grant confirmation, owner digest, canary, hook decision output (relay change).
- Harness adapters for Codex/OpenCode/Pi (pi-engineering), email/spec ingestion, dispatcher.

## Self-review

- Spec coverage: 6.1 -> Tasks 1-3; 6.2 -> Tasks 4-6 (activation implemented, gated); 6.3 -> Task 6 (steps 4/6 collapse, noted); 6.4 -> Task 3; 5.3 `action_request` -> Task 7; 5.4 owner-only -> Task 8; section 8 audit -> Tasks 6, 9; scope opt-in (final-review finding) -> Task 7. Gaps by design: judge, hook-attested evidence, assignments tools.
- Type consistency: `ActionKey`, `Derived`, `RawAction`, `Decision`, `ActionGrant`, `GrantState` names are used identically across tasks.
- Proportion: 9 tasks; code is specified by signatures and tests only.
