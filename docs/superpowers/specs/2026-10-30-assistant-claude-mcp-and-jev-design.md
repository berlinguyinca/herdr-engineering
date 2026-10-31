# Personal assistant as hub for Claude installations: MCP server, learned grants, Jev judge

Status: DRAFT for owner approval. Nothing here is built. Date: 2026-10-30.
Owner statements are quoted; everything marked **(my call)** is a builder-side default the owner has not yet agreed to.

## 1. Intent and why

The owner runs several Claude Code installations (workstation, laptop, sessions that reach HPC login nodes).
Today each one stops at the same decision points and asks the owner: "wait for the PR?", "deploy?",
"draft the artifact for Oliver?". Studies cited in section 9 put manual approval at ~93% accept, i.e. the
prompts are mostly rubber-stamped and still cost attention.

The owner's model, in their words: *"i want the assistant to tell claude what to do, approve things for me and
learn based on what i do. So its an agent to my claude installations communications."*

So the personal assistant (the `control_plane.personal_assistant` feature of herdr-engineering) becomes the **hub**,
and Claude installations are **workers** that reach it over MCP. The assistant:

1. gives workers assignments and answers their questions;
2. approves on the owner's behalf, within **learned, scoped, expiring grants**;
3. learns from what the owner decides and reports.

Learning is driven by a judge: *"we should utilize jev (system one) as a service to judge the probability of it
being dangerous or destructive and generate a new dynamic grant. When this gets granted several times, then we can
autonomously continue."* Jev support must be first class: *"including configuration, key registration and so on"*.
Access: *"The assistant can only be accessed from the same tailscale network and we should have an endpoint for it and a
simple way to register with it."* On the grant model: *"it can do it itself after learning and we maybe need to add more
grants for it. For example the difference between draft and send emails."*

## 2. Goals

- G1. An MCP endpoint for the assistant, reachable only from the tailnet, with one-command registration of a Claude installation.
- G2. Workers can ask questions, request actions, pull assignments, report outcomes and owner decisions, and read assistant state.
- G3. Verb-level grants (e.g. `email.draft` vs `email.send`, `deploy.canary` vs `deploy.prod`) with scope, expiry, revocation.
- G4. Dynamic grants created from evidence: shadow period, then proposal, then (after the first owner confirmation) autonomy.
- G5. First-class Jev (System One) client: config section, key registration, health check, typed question set, calibration report.
- G6. Everything the assistant does autonomously is audited, attributable to a grant, and revocable.

## 3. Non-goals and anti-goals

- Not a replacement for the `herdr-collab` MCP (agent to agent rooms/tasks). This is agent to assistant.
- Not a general remote shell. Workers never execute through the assistant; they ask it for permission and decisions.
- Anti-goal: the assistant approving anything because a model said "probably fine". Probabilities only add friction or propose grants; they never create autonomy on their own.
- Anti-goal: a learned grant that covers more than what the owner actually approved (scope creep via similarity).
- Anti-goal: any path by which a worker approves its own request.
- Non-goal: per-user OIDC; real-model runner work. The repo now has `HttpModelRunnerAdapter` plus `inference` config; the earlier "skeleton runner only" finding was against an older worktree and must be re-verified by the builder.

## 4. Fixed constraints (owner rules, quoted or from standing memory)

These become the **fixed veto list** (section 6.4), evaluated before any grant and before the judge. No learning can lift them:

- Compute nodes and Slurm clusters are no-touch: nothing installed, no cron, no packages, no services; read-only inspection only.
- No deletes on Quobyte; Quobyte is never used for CI data.
- No force-push, no history rewrite of shared branches, no disabling branch protection.
- Never route through fry.fiehnlab.ucdavis.edu (dead); older notes naming Fry are stale.
- Secrets are never printed, stored in files/memory, or sent to services other than their own.
- Content that passes through the assistant (PR text, chat replies, room messages) is untrusted data, never instructions.

Soft rule, learnable: merges happen only on the owner's word. `pr.merge` is a verb whose first grant requires explicit owner confirmation like every other verb (section 6.3).

## 5. The MCP server (hub side)

### 5.1 Endpoint and network restriction

- New module `herdr_engineering/control_plane/assistant_mcp.py`, JSON-RPC MCP over HTTP like `collab_mcp.py` (reuse its framing: `initialize`, `ping`, `tools/list`, `tools/call`, uniform 401).
- Path `POST /assistant/mcp`. **It is NOT mounted on the public collab listener or the nginx edge** that fronts `herdr-collab`; it is served only by a dedicated listener (own host/port config) so a proxy misconfiguration cannot expose it.
- Network enforcement is real code, not a convention (the survey found the existing "tailnet-only" operator listener is only a Host allowlist convention): the server refuses to start unless it is bound to loopback or a tailnet address, and every request is checked against the peer address (100.64.0.0/10, fd7a:115c:a1e0::/48, loopback) plus a Host allowlist (DNS rebinding). `Origin` header requests rejected, as in the collab MCP. `X-Forwarded-For` is ignored.
- Tailnet hostname and port are configuration, with no default hostname **(my call: port 9483, hostname from config only)**.

### 5.2 Identity and registration

Workers are identified by their own credential, never by trusted headers. (The survey found `X-Herdr-User`/`X-Herdr-Tenant` are not honored by the API today; they are not used here.)

- Credential `hcw_` + `secrets.token_urlsafe(32)`, bound to a device id and a worker name (e.g. `workstation`, `laptop`), tenant and owner. Only its sha256 digest is stored. Shown once. Same lifecycle as the collab `hca_` credential: expiry, revoke, uniform 401, audited failures.
- Registration: the owner opens the assistant UI or runs `herdr assistant worker enroll --name workstation`, which prints a one-time enrollment code (`hce_...`, short TTL, single use) and the exact line to run on the worker:
  `claude mcp add --transport http herdr-assistant https://<tailnet-host>:<port>/assistant/mcp --header "Authorization: Worker <cred>" --header "X-Herdr-Device: <device>"`
  (the worker redeems the code via `POST /assistant/enroll` and receives the credential; the owner never copies a long-lived secret through chat).
- A worker has a scope set (default read + ask + report). Granting more is an owner action.

### 5.3 Tool catalog (worker-facing)

All results carry `untrusted: true` markers on any text that originates outside the owner, as `collab_mcp._text` does.

| Tool | Purpose | Notes |
|---|---|---|
| `assistant_ask` | Put a decision point to the assistant: `question`, `options[]`, `context`, optional `action` (canonical form, 6.1) | Returns `answered` (choice + reason + source `grant`/`owner`/`rule`), `pending` (id), or `declined`. Never auto-answers inside the shadow period or when a veto/ungranted sensitive verb applies. |
| `assistant_wait` | Long-poll a pending ask/approval | Results are poll-only today; bounded wait like `tasks_wait`. |
| `action_request` | Ask permission to perform a canonical action | Wraps `request_tool_action`; returns `authorized`/`denied`/`pending`. |
| `assignments_next` / `assignment_update` | Pull the next assignment, report progress and result | Assignments are rows created by the owner or a routine; backed by the existing delegation/proposal machinery where it fits **(my call)**. |
| `report` | Record what happened and what the owner decided/said: `decision`, `correction`, `rule_stated`, `outcome` | The learning feed. A `report` of "owner approved X" is evidence, never an approval; approvals are only decided by the owner through the owner API (5.4). |
| `memory_search` / `memory_write` | Existing assistant memory | `memory_write` marked untrusted-provenance unless source is `owner`. |
| `fleet_read`, `threads_read`, `runs_read`, `routines_list`, `connectors_list`, `delegations_list`, `proposals_list`, `grants_list`, `audit_read`, `approvals_list` | Read-only mirrors of assistant state | Per-worker scope controlled. |
| `judge_status` | Read-only: judge health, mode (shadow/active), calibration summary | |

### 5.4 What workers cannot do (decision, **my call**: flagged for owner)

The owner asked for "access to all its functionality". The hub model changes that: if a worker could decide approvals, create grants, register connectors or change the judge, then a compromised or prompt-injected worker controls its own leash. So:

- Owner-only (owner API key / UI, not the worker credential): `decide_approval`, `grant_tool`/dynamic-grant confirmation, `revoke`, connector registration, judge configuration/key registration, worker enrollment and revoke, veto-list changes.
- Everything else is available to workers as the table above. The owner can widen the worker scope set per worker.

## 6. Verb-level and learned grants

### 6.1 Canonical action

Strings are never matched (allowlist-bypass CVEs in Roo Code/Cursor came from prefix matching shell strings). A worker submits, and the assistant derives, a canonical **ActionKey**:

`(tool, verb, resource_scope, target, flag_class, risk_class)`

- `verb` comes from a catalog (`email.draft`, `email.send`, `pr.comment`, `pr.merge`, `deploy.canary`, `deploy.prod`, `git.push`, `git.force_push`, `artifact.create`, `shell.read_only`, `shell.mutating`...). Unknown verb => owner decides.
- `resource_scope` is resolved: repo `org/name`, recipient identity, environment, resolved path (symlinks followed), host/cluster. No wildcards inside a learned grant.
- `flag_class` buckets flags that change blast radius (`--force`, `--hard`, `-rf`, `--no-verify`).
- Shell commands are parsed (AST, `&&`, `;`, newlines, pipes, subshells, env changes); any element mapping to a mutating or unknown verb makes the whole action that verb. Unparseable => owner decides.
- The existing grant `scope` string stays; learned grants add `verb` and a structured `key`, and match only on exact canonical key equality plus explicitly listed generalizations (e.g. a recipient allowlist).

### 6.2 Grant states

`shadow` -> `proposed` -> `active` -> `expired` | `revoked`.

- Fresh install: **global shadow period** (default 14 days **(my call)**, owner-configurable, owner-visible countdown). In shadow the assistant never answers alone; it records what it *would* have decided next to what the owner decided.
- An ActionKey class becomes `proposed` once it has **N consistent owner approvals** with **diversity**: at least 3 distinct worker sessions and 2 distinct tasks, counted once per session/task so one agent repeating a request cannot farm the count.
- Defaults by risk class **(my call; research supports ~30-100 approvals for a few-percent bound on irreversible actions, there is no authoritative number)**: local reversible N=5; remote reversible (comment, draft, push to feature branch) N=10; outward or hard to reverse (send, merge, deploy) N=30. Owner-configurable per verb.
- **First grant of any verb requires explicit owner confirmation** (a single one-time approval of the *grant itself*). Approvals are noisy labels (rubber-stamping), so evidence alone never activates a grant.
- Activation also needs the judge gate (section 7.4) satisfied for that class.
- Grants are scoped to (worker identity, project/repo), carry an expiry (default 30 days, re-earned or renewed by owner), and reset to `shadow` when the judge model, thresholds or policy version change (new evidence required).
- Owner can revoke any grant at any time; revocation takes effect on the next `action_request`.

### 6.3 Decision order for every `assistant_ask`/`action_request`

1. Fixed vetoes -> deny (audited). Not influenced by judge or grants.
2. Worker credential/scope check.
3. Active grant for the exact key? If yes and the judge (when available) does not escalate => `authorized`, audited with grant id and judge scores.
4. Judge escalates (high P(destructive)/P(irreversible), or scope not covered) => owner approval.
5. No grant => owner approval (shadow evidence recorded).
6. Judge unavailable => owner approval (**fail closed**).

The judge can only add friction or propose grants. It cannot lower a requirement.

### 6.4 Fixed vetoes

Hard-coded in a module that is evaluated first, extended (never reduced) by config, and covered by tests that try to grant around each entry. Seeded from section 4.

## 7. First-class Jev support

Jev = InferWeave System One (`POST /v1/systemone`, API v0.2.0). Its `noul` answer is P(yes), `score` gives probabilities over ordered levels, `choice` over options. It is deterministic-mock by default in the gateway; the Laya backend has run on a 4090 but whether production serves Laya, and whether its checkpoints say anything about command danger, is **unverified**. That is why the shadow period and calibration gate exist.

### 7.1 Configuration

New section `control_plane.personal_assistant.judge`:

```yaml
judge:
  enabled: false
  provider: jev          # only value for now; leaves room for other judges
  endpoint: https://llm.metabolomics.us/v1/systemone
  model: laya-auto
  api_key_file: /etc/herdr/judge.key   # path only, never inline (matches audit.hmac_key_file / apps.key_file)
  timeout_seconds: 5
  fail_mode: closed      # only value
  thresholds:
    destructive: 0.30    # P(yes) at or above => escalate to owner (my call)
    irreversible: 0.30
    severity_score: 2.0
  calibration:
    min_labeled_decisions: 200
    max_false_negative_bound: 0.05   # 95% upper bound, per risk class
```

The existing `inference.api_key` pattern (plain text in YAML) is explicitly not copied. Unknown keys under `judge` raise, like `ci_watch` and `apps`.

### 7.2 Key registration

- `herdr assistant judge register --endpoint ... --key-stdin` writes the key to a file with mode 0600 in the configured secrets directory, records the path in config, performs `GET /v1/models` as a health check, and prints only the key's fingerprint. Never echoes the key.
- Equivalent owner-only HTTP route `POST /api/v1/assistant-judges` for the UI.
- Key rotation: re-run register; old file kept until the new key passes the health check. Redaction: key never enters audit, logs, snapshot or MCP output (add the file path pattern to `redact_obj` tests).

### 7.3 Client and question set

- `JevClient`: pooled HTTP client, bearer auth, timeout, typed `JudgeResult` (probabilities preserved, never collapsed to booleans), response validation, error translation (422/5xx/timeouts => `JudgeUnavailable`).
- One request per action with questions: `destructive` (noul), `irreversible_or_external` (noul), `severity` (score: informational/minor/major/critical), `owner_approval_covers` (noul: does a prior approval cover exactly this key).
- Input is the **canonical ActionKey plus owner-authored context only**. Tool outputs, PR bodies and chat text are never sent to the judge (injection defense; matches the published design of Anthropic's auto-mode classifier).

### 7.4 Shadow recording and calibration gate

- Every decision stores `(action_key, judge_probs, judge_version, owner_decision, worker session/task)`.
- A calibration report (reliability bins, false-negative count, one-sided upper bound by Clopper-Pearson at 95%) is produced per risk class. A class may leave `shadow` only when `labeled >= min_labeled_decisions` and the bound is under `max_false_negative_bound`. Report is readable via `judge_status` and the owner UI.
- The report states plainly when labels are the owner's approvals (noisy), and runs a periodic **canary**: a synthetic, clearly-dangerous prompt to the owner to measure rubber-stamping **(my call, can be dropped)**.

## 8. Audit and safety

- Every autonomous action: audit record with grant id, canonical key, judge version/probabilities, worker identity. Uses the existing hash-chained `audit_record`.
- Owner digest of autonomous actions since last check (read via `audit_read` and UI).
- Self-approval stays refused (`allow_self_approval` false); worker credentials can never be listed in `actions.approvers`.
- Rate limits per worker; `action_request` idempotent as today.
- Replies delivered to workers are phrased as decisions with reasons, not as new instructions from the open internet.

## 9. Evidence behind the choices (secondary sources, partly unverified)

- Claude Code auto mode (Anthropic engineering post): users accept ~93% of prompts; two-stage classifier; full pipeline false-negative 17% on real overeager actions; classifier never sees tool results; denials return to the agent; escalation to human after repeated denials.
- Humans miss roughly 1 in 3 dangerous commands in one observational dataset (409k decisions): approvals are noisy labels.
- Conformal/e-value deferral literature supports a guaranteed bound on auto-approved errors; a rule-of-three gives ~10% at N=30, ~3% at N=100 with zero failures (builder-side inference).
- Allowlist bypasses by newline/pipe/`&&`/builtins (Roo Code, Cursor CVEs) motivate AST canonicalization.
- No authoritative approvals-count standard exists; the defaults in 6.2 are the builder's, marked as such.

## 10. Testing (project policy: TDD, no mocks)

- Unit: canonicalization, veto list (try to grant around each), decision order, counting/diversity, calibration math.
- Integration: real `ControlPlaneServer` in a thread plus real client over HTTP (existing pattern in `test_assistant_api.py`); tailnet source-address rejection with real sockets bound to loopback and a non-allowed source; enrollment/redeem/revoke flows.
- Jev: client parsing tested against recorded real responses from the gateway docs; integration tier runs against a real `iw-gateway` with its in-process backend (marker `integration`, builder decides how CI obtains the binary). No fake Jev server.
- Smoke: `claude mcp add` snippet against a running server (`initialize` + `tools/list`).
- E2E: worker asks -> pending -> owner approves in the UI (Playwright) -> worker resumes.
- Note: the repo has no `.github/workflows`; coverage and CI enforcement route is unknown and is left to the builder to establish with the owner.

## 11. Phasing (each phase its own branch/PR, independent review, no merge without the owner's word)

1. MCP server + enrollment + network enforcement + read tools + `assistant_ask`/`report` (all owner-answered, shadow recording only).
2. Verb catalog, canonicalization, fixed vetoes, verb-level grants.
3. Jev client, `judge` config, key registration CLI/route, shadow recorder, calibration report.
4. Dynamic grant proposal/confirmation/activation, expiry/reset, owner digest.

## 12. Left to the builder

Module layout, JSON-RPC details, exact schemas, storage layout (new collections in `AssistantStore` snapshot and a migration if Postgres is used), CLI placement, UI screens, the CI route, and the integration-test harness for `iw-gateway`.

## 13. Calls made after the builder check (builder-side, owner to confirm)

- **Transport / tailnet (my call).** The worker listener serves plain HTTP bound to the node's tailnet interface address (WireGuard already encrypts the hop), or loopback for local use. No reverse proxy and no `tailscale serve` in front, so the peer-address check in 5.1 is meaningful (a proxy would make every peer look like loopback). The credential is still required. If a proxy is ever added, the peer check must be replaced by a verified-identity header from that proxy; that is out of scope here.
- **Two trust domains (my call).** The worker listener hosts only worker routes (`/assistant/mcp`, `/assistant/enroll`). Owner-only routes (decide approval, confirm grant, revoke, judge register, worker enrollment/revoke) stay on the existing operator API listener with the existing owner API key and the existing Personal Assistant UI (approvals tab). A worker credential can never reach an owner route.
- **Evidence trust (my call).** Decisions made through the owner API/UI count fully toward N. Decisions only *reported* by a worker (`report`) are stored as `worker_reported`, are forgeable, and count for nothing toward activation; they feed learning suggestions and the shadow comparison only.
- **ActionKey derivation (my call).** The worker sends the raw command/action plus an optional worker-side hint (resolved paths, recipient). The hub re-derives the key from the raw text itself and, if hint and derivation disagree, uses the stricter. Hub cannot resolve remote symlinks, so path facts from the worker are marked `worker_attested`; a compromised worker is not defended against beyond this (it already holds its own tools). First-version command families: git, gh, ssh/srun/sbatch/scancel, email, deploy tooling. Anything else: owner decides.
- **Gate combination (my call).** All gates are ANDed: global shadow elapsed, N and diversity met, judge calibration gate met, first-grant confirmation done. Confirmation is once per verb; a new (worker, repo) scope for an already-confirmed verb needs max(3, N/3) distinct approvals plus a one-tap confirm.
- **Ground truth for the calibration gate (my call).** A judge false negative is: judge scored below threshold (said "safe") and the owner then denied the action, regretted it in the digest, or revoked the resulting grant. Owner approvals are not treated as proof of safety. Phase 3 is built and tested against the gateway's in-process backend; the gate cannot pass, and phase 4 activation is blocked, until the owner confirms a real model is serving and a shadow-period calibration report exists.

## 14. Harness adapters: Claude Code, Codex, OpenCode, Pi (owner request: all four, all in herdr-engineering)

The hub, credential, tool catalog and grant model are harness-independent. `herdr assistant worker enroll --name X --harness claude|codex|opencode|pi` records the harness type (reusing the existing `HARNESSES = ("pi","claude","codex","opencode")` list in `assistant_dispatch.py:38`) and prints/writes the harness-specific setup below. Facts come from a read-only survey on 2026-10-30 against Claude Code 2.1.314, codex-cli 0.164.0, OpenCode 1.18.26 and Pi (pi-coding-agent 0.87.1); items marked *unverified* must be confirmed against the live tool before building that adapter. The repo today has no code that writes MCP config for codex or opencode and no approval-hook adapter; all of it is new.

| | Claude Code | Codex | OpenCode | Pi |
|---|---|---|---|---|
| Register hub MCP | `claude mcp add --transport http ... --header "Authorization: Worker <cred>" --header "X-Herdr-Device: <id>"` (verified) | `~/.codex/config.toml` `[mcp_servers.x]` with `url`, `http_headers`, `env_http_headers` (*unverified*; CLI only has `--bearer-token-env-var`, which cannot send the `Worker` scheme) | `mcp.<name>` `{type:"remote", url, headers}` in `opencode.json`, `{env:VAR}` interpolation (verified in docs) | No native MCP client. A pi-engineering extension registers the assistant tools via `pi.registerTool` and calls the hub over HTTP |
| Route approvals to hub | `PermissionRequest` hook returns allow/deny; `http` and `mcp_tool` handler types exist | `PermissionRequest` hook, allow/deny, `command` and `mcp_tool` handlers | No hook that can approve. `tool.execute.before` can only deny; `permission.asked` is observe-only. SDK reply API *unverified* | `pi.on("tool_call")` can block only; Pi has no interactive approval prompt |
| Hub unreachable | Non-blocking: normal terminal prompt continues (this is the safe fall-through) | unspecified (*unverified*) | plugin throws to deny | handler error blocks (fail closed) |
| Receive assignments | MCP long-poll tools; optional loop skill or Stop-hook continuation | `codex exec` loop or app-server (*unverified*) | `opencode serve` accepts prompts (*unverified*) | extension timer + `pi.sendUserMessage` (*unverified*) |

Decisions **(my call)**:

- **Hook relay is a thin script shipped in the repo** (`herdr assistant worker hook`), called by the harness hook; it reads the harness event, canonicalizes to the hub's action format, calls `action_request`, and prints the harness-native allow/deny JSON. It returns *no decision* (so the harness's normal prompt runs) whenever the hub says `pending`, is unreachable, or the action is a veto-adjacent unknown. It only ever returns **allow** for an `authorized` result backed by an active grant; it returns **deny** only for fixed vetoes. Everything else falls through to the owner's own prompt, which keeps the owner in control during shadow.
- **Order of delivery:** Claude Code and Codex first (native `PermissionRequest` allow/deny). OpenCode: MCP tools + observe-only reporting first; approval relay only if the SDK reply API is confirmed. Pi: extension that registers the tools and gates `tool_call` (fail closed is configurable per worker; default `fall-through-not-possible => block only vetoes, allow rest`, owner to confirm because Pi has no prompt to fall back to).
- **Installer** writes only into the user-level config of the harness being enrolled, shows a diff and requires the owner to confirm; it never edits another harness's config and never touches cluster nodes (login-node-only workers use the voluntary `assistant_ask` path, no installed hook).
- **Evidence still trusted only from the owner API/UI** (section 13). Hook-observed approvals made in the terminal are `worker_reported` and do not count toward activation.
- **Phase 1 is split per harness:** 1a Claude Code, 1b Codex, 1c OpenCode, 1d Pi. Each has its own smoke test against the real tool, run in a throwaway HOME (never the real `~/.claude`, `~/.codex`, `~/.config/opencode` or `~/.pi`).

## 15. Owner decisions of 2026-10-30 (later): hooks yes, learn inside the session, Pi work in pi-engineering

Quoted: *"we are ok with hooks, but we want to do the learning inside the harness session, since we can't jump back and forth"* and *"can we add this functionality in pi-engineering?"*

### 15.1 Learning happens in the harness session (supersedes the UI-only evidence rule in section 13)

The owner keeps answering in the terminal of whichever harness they are in. The assistant learns by **observing** those answers through harness hooks, not through a second UI.

- **Hook-attested evidence counts.** A decision observed by a hook run by the harness itself (not by the model) is `harness_attested` and counts toward N. Basis: a prompt-injected model cannot run or alter the hook process; the hook reports with the worker credential. What the model can call over MCP (`report`) stays `worker_reported` and counts for nothing. Residual risk, stated plainly: a compromised machine can forge attested evidence; that is accepted because it already controls the owner's session.
- **How a hook observes the answer (per harness, to verify):** the `PermissionRequest` hook returns no decision (the normal prompt runs); a post-tool hook (`PostToolUse` / `PostToolUseFailure` or the harness equivalent) reports whether the tool then ran (owner approved) or was refused (owner denied). The hook sends action key, outcome, session id, task id, timestamp.
- **First-grant confirmation also happens in-session.** When a verb/scope becomes `proposed`, the next time that action arises the hook surfaces it through the harness's own prompt with reason text ("Assistant proposes to auto-approve <verb> in <scope> from now on; you approved it N times: <summary>"), e.g. via `PreToolUse` `ask` or MCP elicitation. A yes creates the grant, recorded as `harness_attested` owner confirmation. A model-callable tool can never confirm a grant.
- **Revocation and digest in-session:** a `SessionStart` hook injects a short digest of autonomous actions since last session and how to revoke (a slash command / `herdr assistant grants revoke`). The owner API/UI remains available but is no longer required for learning.
- Consequence for the judge's miss label (7.4): "owner denied or regretted afterwards" is now observed from the same hook feed plus an explicit in-session `/regret` command **(my call)**.
- Consequence for the shadow period: during shadow the hook never allows or denies on its own; it only observes and reports.

### 15.2 Pi adapter lives in pi-engineering (new repo in scope)

The Pi side is built in **berlinguyinca/pi-engineering**, not herdr-engineering. herdr-engineering owns the hub (MCP server, grants, judge, claude/codex/opencode adapters); pi-engineering owns the Pi extension that talks to it.

- **Hard working rules for that repo:** work only in a fresh git worktree/clone off `origin/main` on a new branch. The live install at `/home/wohlgemuth/pi-engineering` (currently `main`, 67 commits behind origin) and `~/.pi/agent` and the real `~/IdeaProjects/pi-eng-*` mission worktrees are never edited or used for tests; tests use a throwaway HOME.
- **Pi has no native MCP client** (survey). The pi-engineering `McpRegistry` today is a stdio-only policy registry with no headers/HTTP transport. Two options, **builder to choose after reading the type declarations**: (a) add a Streamable-HTTP MCP client transport with custom headers to pi-engineering and register the hub like any other MCP server; (b) a dedicated "assistant" extension that registers the assistant tools directly with `pi.registerTool` and calls the hub over HTTP. Preference **(my call)**: (b) first, because it is smaller, and it can later be re-expressed as (a).
- **Extension responsibilities:** register assistant tools (`assistant_ask`, `action_request`, `assignments_next`, `report`, read tools); gate `pi.on("tool_call")` through the hub using the same canonical action format and the same rule (allow only on active grant, deny only on veto, else fall through); post-tool observation for evidence (Pi has no approval prompt, so "owner answered" does not exist in Pi: **Pi workers produce no approval evidence**, only outcomes; learning for Pi actions comes from the other harnesses' answers to the same action keys **(my call, owner to confirm)**); an enrollment command that stores the worker credential in pi-engineering's own config location, never in files in the repo.
- **Fall-through for Pi when the hub is unreachable (open, section 14 question):** with no prompt to fall back to, default **block only fixed vetoes and allow the rest**, configurable per worker.
- **Delivery:** its own phase 1d with its own PRs in pi-engineering; pi-engineering has its own CI (Woodpecker was only just activated and no required checks yet), review and merge rules apply per repo; no merge without the owner's word.
