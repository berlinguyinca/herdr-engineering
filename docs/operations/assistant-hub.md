# Assistant hub (Phase 1): a tailnet-only MCP endpoint for coding agents

The hub lets a coding agent on one of your own machines (today: Claude Code) ask the Personal Assistant a question,
report an outcome, search assistant memory and read a few assistant lists. It is a separate, small listener inside the
control-plane process. It is **off by default**. See also the older
[operator guide](../specs/rakazo-personal-assistant-20261027T193417Z/operator-guide.md) for the assistant itself.

## What Phase 1 does and does not do

Phase 1 is **record only**.

- It records asks (`assistant_ask`), reports (`report`) and hook observations as worker events.
- `assistant_ask` answers nothing: it always returns `{"status":"no_decision","mode":"shadow","event_id":"evt_..."}`.
- It approves nothing, grants nothing, runs nothing and learns nothing from what it records.
- Read-only tools: `memory_search`, `grants_list`, `approvals_list`, `routines_list`, `connectors_list`,
  `delegations_list`, `proposals_list`.
- Learning and grants driven by observed behaviour are Phases 2 and 3 (below); nothing recorded in Phase 1 activates them.

## Enabling the hub

The assistant itself must be enabled too (`personal_assistant.enabled: true`). Then, in the fleet config:

```yaml
personal_assistant:
  enabled: true
  hub:
    enabled: true
    host: 100.64.0.9            # your tailnet address (or 127.0.0.1 for a local-only test)
    port: 9483
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch for the hub listener. |
| `host` | `127.0.0.1` | Bind address. |
| `port` | `9483` | Bind port (1-65535, 0 is refused). |
| `allowed_networks` | `127.0.0.0/8`, `::1/128`, `100.64.0.0/10`, `fd7a:115c:a1e0::/48` | CIDRs a peer must be in (loopback and tailnet). |
| `allowed_hosts` | `[]` | Extra `Host` header names accepted, e.g. your tailnet DNS name. Also used for the host in the setup snippet. |
| `enrollment_ttl_seconds` | `900` | How long an enrollment code stays redeemable. |
| `worker_ttl_days` | `90` | How long a redeemed worker credential lives. |
| `tenant` | the assistant's default tenant | Tenant for the hub. |

Unknown keys or malformed values stop startup.

**Bind loopback or the tailnet address only. Never `0.0.0.0`, never `::`, and never put the hub behind the nginx edge
or the public collaboration listener.** The hub's only protection is network position plus a per-worker credential;
every request is also checked against `allowed_networks` using the real socket peer (forwarding headers are never
read), and a wildcard bind would expose it to networks you did not intend. Use a dedicated port reachable only on the
tailnet interface.

## Enrolling a Claude Code worker

1. **Enroll (owner, on any machine that can reach the control plane).**
   `herdr-eng control-plane assistant-worker enroll --control-plane http://CP:PORT --api-key-file KEYFILE --name laptop --harness claude`
   (or `POST /api/v1/assistant-workers` with an operator API key). You get a one-time code and a `setup` snippet.
   The code is shown once and expires after `enrollment_ttl_seconds`.
2. **Redeem (on the worker machine).**
   `herdr-eng control-plane assistant-worker redeem --url http://HUB_HOST:9483 --code CODE --device-id my-laptop`
   writes `~/.config/herdr/assistant-worker.json` (mode 0600; the parent directory is created 0700) and prints the
   `claude mcp add` line once. Plain `http://` to a non-private host prints a warning. The code is single use.
   A `--code` value is visible to other local users in `ps`. Prefer `--code -` (reads the code from stdin) or set
   `HERDR_ASSISTANT_ENROLL_CODE` and omit `--code`; an explicit `--code` still works and wins over the env var.
3. **Register the server with Claude Code.** Run the printed line:
   `claude mcp add --transport http herdr-assistant http://HUB_HOST:9483/assistant/mcp --header "Authorization: Worker <credential>" --header "X-Herdr-Device: my-laptop"`
4. **Install the observe-only hooks (optional).**
   `herdr-eng control-plane assistant-worker install-hooks --harness claude --settings ~/.claude/settings.json`
   shows a diff and asks before writing (`--yes` skips the prompt; a timestamped backup is kept). It adds
   `PermissionRequest`, `PostToolUse` and `PostToolUseFailure` hooks that post what happened to
   `/assistant/hook-events`. The hook command is wrapped so it exits 0 with no output, never blocks a tool and never
   changes a permission decision. It is idempotent and only ever replaces its own entries.

`assistant-worker list` shows workers (no secrets). Only the `claude` harness has setup support in Phase 1.

## Scopes and what an enrolled worker can read

A redeemed worker gets the default scopes `ask` and `report`. The optional `read` scope gives it read access to the
owner's assistant memory (`memory_search`) and to the owner's grants, approvals, routines, connector metadata (never
credentials), delegations and proposals. That is a lot of private context for one credential, so **enroll only
harnesses you trust, and consider narrowing a worker's scopes** (a worker without `read` cannot use the list or search
tools).

## Provenance

| Provenance | Source | Meaning |
|---|---|---|
| `worker_reported` | MCP tools (`assistant_ask`, `report`) | The model chose to say this. Treat it as untrusted text. A caller cannot select a different provenance. |
| `harness_attested` | `/assistant/hook-events` | Posted by the harness hook, not by the model's tool call. |

**Known limit.** The hook relay authenticates with the same worker credential file the machine holds. A model with shell
access on that machine can read the 0600 file (it runs as your user) and post events that look `harness_attested`.
Phase 1 evidence activates nothing, so this cannot cause an action today. Before any later phase lets attested evidence
count (grants, learning, judging), it must add a hook-only secret or a stronger channel (for example a credential the
model's shell cannot read).

## Owner-only versus worker-reachable

- **Owner only** (operator API key, control-plane port): `POST/GET /api/v1/assistant-workers`,
  `DELETE /api/v1/assistant-workers/<id>`. A worker credential gets 401/403 here, and a viewer key gets 403.
- **Worker reachable** (hub port): `POST /assistant/enroll` (a code, not a credential), `POST /assistant/mcp` and
  `POST /assistant/hook-events` (worker credential plus `X-Herdr-Device`).

## Revoking a worker

`herdr-eng control-plane assistant-worker revoke --control-plane http://CP:PORT --api-key-file KEYFILE WORKER_ID`
(or `DELETE /api/v1/assistant-workers/<id>`). The credential gets 401 on `/assistant/mcp` and `/assistant/hook-events`
immediately. Also remove the server from the worker (`claude mcp remove herdr-assistant`) and the hooks from its
settings file. Revocation and credential use are audited; the audit log and store snapshot hold only digests, never the
credential or the enrollment code.

## Limits

- Event payload: 4 KiB after scrubbing (secret-looking values are masked before storage).
- Retention: 1,000 events and 4 MiB per tenant (oldest dropped first).
- Per-worker event limit: 30 events/min per worker across the event-writing paths (`assistant_ask`, `report` and
  `/assistant/hook-events`), in addition to the per-peer limit. Beyond it the hook endpoint returns 429 and the MCP
  tools return the tool error `rate limited`. Every event persists a snapshot, so this keeps a runaway worker from
  stalling the store.
- Repeated authentication failures for the same worker and reason (revoked, expired, device mismatch) are audited once
  per 60 s window, like unknown credentials; later windows record how many were suppressed.
- The persisted store snapshot file is written with mode 0600.
- Rate limits per peer address: 120 requests/min for `/assistant/mcp` and `/assistant/hook-events` combined, 10/min for `/assistant/enroll` (429 beyond).
- At most 8 concurrent connections per peer; a request must finish within 15 s (slow clients are cut off).
- Request bodies over 64 KiB are refused (413).

## Phase 2: action requests, vetoes and earned grants

Phase 2 adds one worker tool, `action_request`, and the owner-side machinery behind it. A worker asks "may I run this?"
before it acts and gets a **decision with a reason** (`authorized`, `denied` or `pending`), never an instruction.
**Nothing is autonomous by default**: a request can only come back `authorized` through an `active` grant, and a grant
can only be activated after the Phase 3 judge gate passes (see "Phase 3" below and "Why nothing is autonomous yet").

### Verbs and what an ActionKey binds

A request (`tool`, `command`, `operation`, `recipients`, `repo`, `environment`, optional `hint`, plus `session_id` and
`task_id`) is canonicalized into an **ActionKey**: a verb (for example `git.push`, `pr.merge`, `deploy.prod`,
`email.send`), a resource scope, a risk class (`local_reversible`, `remote_reversible`, `outward`) and a flag class.
The key id is a hash of the canonical form, and a grant matches on that exact id, tenant, user and worker.

- Only a **single plain command** gets a known key. The key binds the **full argv** (as a digest), so
  `git push origin main` and `git push -u origin feature` are different keys.
- **Chains** (`&&`, `;`, pipes), **redirects**, **env prefixes**, **wrappers** (`env`, `nohup`, `ssh host cmd`, `sh -c`),
  `sudo`, **dynamic words** (`$VAR`, `$(...)`, globs) and **unknown heads** always go to the owner (`pending`, source
  `unknown`). The canonicalizer is an allowlist of known heads; everything else is unknown and fails closed.
- Unknown verbs are always `outward` risk.

### The fixed veto list

A veto is evaluated first, over every element of the command, and wins over any grant. The response carries
`outcome: denied`, `source: veto` and a `veto_code`; the row is stored with status `vetoed`, cannot be decided by
anyone (409), and the audit log gets `assistant.action.vetoed`.

| Code | Blocks |
|---|---|
| `force_push` | force push (`--force*`, `-f`, `+refspec`), mirror push, remote ref deletion (`--delete`, `:ref`) |
| `protection_disable` | changing branch protection or rulesets (for example via `gh api` or GraphQL mutations) |
| `quobyte_delete` | deleting or modifying anything under `/quobyte` |
| `fry_route` | routing through the retired Fry host |
| `cluster_install` | installing software or editing services on a cluster |
| `secret_print` | printing secrets, keys or tokens (for example `gh auth token`, `kubectl get secret`, secret variables) |

`grants.extra_vetoes` can add rules (`code`, `reason`, and `verb` and/or `argv_contains`); it can never relax or reuse a
fixed code.

**Vetoes are a best-effort backstop, not a sandbox.** The real control is that nothing unlisted is ever authorized
without an owner-confirmed grant. Known residual gaps: commands embedded in `python -c` or other interpreter strings,
the body of an `sbatch` script, variable indirection with no literal `quobyte` in the text, `rsync host::module`
targets, and `cat $(...)` style substitutions. Most of these are also `pending` (unknown head or dynamic word), so
the owner still decides; a veto simply cannot catch them on its own.

### Grant states and defaults

States: `shadow` (arises only from a `policy_version` mismatch; the GLOBAL shadow period does not change a grant's state, it acts through the shadow check: the grant keeps its state and requests are `pending` with source `shadow`), `proposed`
(evidence suffices, waiting for the owner), `active`, `expired`, `revoked`. The state shown is always the effective
state computed now.

| Key (`personal_assistant.grants`) | Default | Meaning |
|---|---|---|
| `shadow_days` | `14` | Global shadow period; no grant is honoured and none can be activated before it ends. |
| `shadow_started_at` | `null` | ISO-8601 with offset. `null` = stamped lazily at the first `action_request` or activation attempt (not at hub start), then persisted. |
| `required_approvals` | `local_reversible 5`, `remote_reversible 10`, `outward 30` | Owner approvals of the exact key needed per risk class. A new scope for an already confirmed verb needs `max(3, n/3)`. |
| `min_sessions` | `3` | Distinct `session_id` values among the counted approvals. |
| `min_tasks` | `2` | Distinct `task_id` values among the counted approvals. |
| `grant_ttl_days` | `30` | Lifetime of a proposed grant, and of an active one from activation. |
| `extra_vetoes` | `[]` | Extra hard-deny rules (see above). |
| `policy_version` | `1` | Bump to invalidate every existing grant (they show as `shadow` until re-earned). |

### Evidence rules (exact)

- Only **the owner's decisions made through the owner API** (`provenance owner_api`) count. A worker's `report`,
  `assistant_ask` and hook events count for nothing, however many it sends.
- Evidence is per (worker, key). A **denial restarts the streak**: approvals before the latest denial are ignored.
- Approvals must come from enough distinct sessions and tasks. **Known limit:** a worker can invent session and task ids,
  so diversity only protects against one agent repeating itself; every counted approval is still the owner's own.
- A vetoed or unknown-verb action never earns a proposal. Evidence older than a revoked or expired grant does not count
  towards the next proposal.
- Meeting the thresholds only creates a `proposed` grant. It is not usable.

### Why nothing is autonomous yet

- **BREAKING from Phase 2:** an `authorized` answer now also needs a judge that is enabled, `backend_confirmed` and
  reachable; otherwise `pending` (source `judge_unavailable`). Phase 2 activation was hard-blocked, so no `active`
  grants should exist. Unsetting `backend_confirmed` or disabling the judge immediately stops autonomy for existing
  `active` grants.
- **Phase 3** (below) adds the Jev judge and its calibration gate. `POST /api/v1/assistant-grants/<id>/activate` checks, in
  order: 404 `not_found`, 409 `not_proposed`, `still_in_shadow`, `confirmation_missing`, then the judge gate codes
  listed in Phase 3. Every refusal except `not_found` (an unknown id has no grant to attach it to) writes
  `assistant.grant.activate_refused`. With no judge configured the answer is
  `judge_disabled`, so a default install can never reach an active grant.
- **Phase 4** (below) adds the Slack front door: the owner's verified tap is owner evidence (`owner_slack`), and a
  per-project, per-scope deploy permission replaces the approval count for a deploy the owner confirmed. Hook-attested
  evidence is still deferred, and until it exists the Phase 1 known limit above (a model with shell access can post
  `harness_attested` events) stays open, so attested evidence does not count.
- Even an active grant is ignored during the global shadow period (`pending`, source `shadow`).

### Scopes

| Scope | Gives |
|---|---|
| `ask` | `assistant_ask` |
| `report` | `report` |
| `read` | memory search and the read-only lists, plus `action_grants_list` |
| `act` | `action_request` |

New workers get **`ask` + `report` only**; `read` and `act` are opt-in. Set them at enrollment
(`POST /api/v1/assistant-workers` with `"scopes": ["ask","report","read","act"]`, or
`herdr-eng control-plane assistant-worker enroll ... --scopes ask,report,read,act`) and change them later with
`POST /api/v1/assistant-workers/<id>/scopes` (`herdr-eng control-plane assistant-worker scopes WORKER_ID --scopes ...`).
The new set applies to the very next `tools/list` and `tools/call`.

### Owner routes and CLI (operator API key only)

A worker credential gets 401/403 on all of these, a viewer key gets 403, and cross-origin mutations are refused.

| Route | CLI (`herdr-eng control-plane ...`) | Purpose |
|---|---|---|
| `GET /api/v1/assistant-action-requests[?status=pending\|approved\|denied\|vetoed]` | `assistant-action list [--status S]` | Requests, including `display`. |
| `POST /api/v1/assistant-action-requests/<id>/decision` `{"approve": true\|false}` | `assistant-action approve\|deny ID` | Record the owner decision (counts as evidence). 409 if already decided or vetoed. |
| `GET /api/v1/assistant-grants` | `assistant-grant list` | Grants with their effective state. |
| `POST /api/v1/assistant-grants/<id>/activate` `{"confirm": true}` | `assistant-grant activate ID` | 409 with the gate code until the judge gate passes (Phase 3). |
| `DELETE /api/v1/assistant-grants/<id>` | `assistant-grant revoke ID` | Revoke; the next request is `pending`. Idempotent. |
| `POST /api/v1/assistant-workers/<id>/scopes` `{"scopes": [...]}` | `assistant-worker scopes ID --scopes ...` | Change a worker's scopes. |

### The `display` field

`display` is what lets you recognise a request. It is returned **only** on these owner routes (and held in the owner's
request row and store snapshot); it is never in the MCP response, the audit log, grants, evidence or events. It is
**masked best effort** (URL userinfo, secret-named options and `NAME=value` pairs, Bearer headers, token shapes), then
truncated to 300 characters with control characters removed. A scrubber cannot recognise an arbitrary secret word
(`mysql -pSomething`-style variants and bare passwords can slip through), so treat the owner request list as sensitive
and keep it off shared screens.

### Hint trust rule

`hint` (`cwd`, `repo`, `recipient`, `host`) is worker-supplied and never trusted to widen anything. A hint fact that the
canonicalizer actually used is recorded as worker-attested, and **a hint that disagrees with what the command itself
says makes the owner decide**. A hint is ignored once the command redirects git elsewhere (`cd`, `git -C`, `--git-dir`).
Hints can make a request stricter, never more permissive; they cannot clear a veto.

### Persisted-file trust boundary

The store snapshot file holds worker credential digests, the audit log, grants, requests, evidence and `policy_state`.
Nothing in it is signed against the host: anyone who can write that file can forge state, including an active grant.
Protect it with mode 0600 and treat host access as the trust boundary.

### Audit action names

`assistant.action.requested`, `assistant.action.vetoed`, `assistant.action.authorized`, `assistant.action.decided`,
`assistant.grant.activated`, `assistant.grant.activate_refused`, `assistant.grant.revoked`,
`assistant.worker.scopes_changed`, plus the Phase 1 worker entries (`worker.enrolled`, `worker.redeemed`,
`worker.auth_failed`, `worker.revoked`). Entries carry the key id and ids only: no command text, credentials or enrollment codes. The chain verifies with
`herdr-eng control-plane assistant-audit verify`.

## Phase 3: the Jev judge, shadow records and the activation gate

Phase 3 connects the hub to a **System One** (Jev) judge. The judge can only **add friction**: it never authorizes
anything by itself. An action is `authorized` only when an `active` grant covers the exact key AND the judge answers
AND does not escalate. Before that, the judge runs in **shadow**: it is asked about every new `pending` request, the
answer is stored next to the owner's real decision, and those pairs are the evidence for the gate.

> **Upgrading from Phase 2 (breaking change).** Any request that Phase 2 would have answered `authorized` now comes
> back `pending` with source `judge_unavailable` unless a judge is **enabled, `backend_confirmed: true` and reachable**.
> Phase 2 activation was hard-blocked, so no `active` grants should exist on an upgraded install. Conversely, unsetting
> `backend_confirmed` or disabling the judge takes effect on the very next request and immediately stops autonomy for
> every existing `active` grant.

### Registering the key (CLI only)

There is deliberately no HTTP route that accepts a key: secrets never travel in request bodies.

```
printf '%s' "$KEY" | herdr-eng control-plane assistant-judge register \
    --endpoint https://llm.example.org --key-file /etc/herdr/jev.key --key-stdin [--model laya-auto]
```

- The key is read from **stdin only** (never argv; a TTY gets a hidden prompt). It is never printed; the command prints
  only `registered: fingerprint=<8 hex of sha256> model=<id> models_seen=<n>`.
- The file is created `0600` with `O_EXCL` at `PATH.new`, health-checked with a real `GET <endpoint>/v1/models`, checked
  for the configured model id, and only then moved to `PATH`. A failure removes `PATH.new` and leaves an existing `PATH`
  untouched. **Rotation** is the same command: the old key stays in place until the new one has proved itself.
- The parent directory must not be group- or world-writable (`unsafe_dir`), `PATH` must be absolute and not a symlink,
  the endpoint must be `https` (`http` only for loopback, so a bearer key never crosses a network in clear text).
- Failures print `assistant-judge register failed: <code>` (for example `unsafe_dir`, `tmp_exists` when a stale
  `PATH.new` is left over, `bad_path`, `symlink`, `bad_key`, `config`, or `model_not_listed`, which also prints the first
  ten model ids the server offered). Exit codes: 0 ok, 1 refused, 2 usage.
- The command does **not** edit the YAML. Add the block below yourself.

### The YAML block

`personal_assistant.judge` in the fleet config (a commented copy with the defaults is in
`deploy/control-plane/fleet-config.example.yaml`):

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Off = no consults, no shadow records, `judge_disabled` at the gate. |
| `provider` | `jev` | The only value. |
| `endpoint` | `""` | `https` URL; `http` only for loopback. Required when enabled. |
| `model` | `laya-auto` | Must be served by `GET /v1/models`. Part of the judge version. |
| `api_key_file` | `""` | Absolute **path** of the key file. Inline `api_key`, `key`, `token` are rejected at load. |
| `timeout_seconds` | `5` | Integer 1..60, applied to each authorize-path consult. |
| `fail_mode` | `closed` | The only value: judge unavailable means the owner decides. |
| `backend_confirmed` | `false` | Owner attestation, see below. |
| `thresholds` | `destructive 0.30`, `irreversible 0.30`, `severity_score 2.0` | The judge escalates when any answer reaches its threshold (or when "would the owner's approval cover this" is below 0.5). |
| `calibration` | `min_labeled_decisions 200`, `max_false_negative_bound 0.05` | The gate; the minimum cannot be set below 20; the bound must satisfy 0 < x <= 0.5. |

Validator rules (a bad value refuses to load the config): `endpoint` is `https`, or `http` for loopback hosts only, with no
userinfo, query, fragment, whitespace or control characters; `thresholds.destructive` and `.irreversible` satisfy
0 < x <= 1, `severity_score` 0 <= x <= 3; `timeout_seconds` is an integer 1..60; `api_key_file` is absolute; unknown
keys are rejected.

### `backend_confirmed`: why the response cannot prove a real model

The System One mock backend and a real Laya serve the **same documented response shape** and the same model ids, so
nothing in a reply tells you which one answered. A judge backed by a mock would pass every calibration check
trivially and then wave things through. So the gate refuses (`backend_unconfirmed`) until you set
`backend_confirmed: true`, and you set it only after verifying out of band: `GET /v1/systemone/runtimes` on the
System One host, or ask whoever runs the worker. Treat it as an attestation, not a probe.

### What is sent to the judge, and what never is

Sent: five facts of the ActionKey: `tool`, `verb`, `resource_scope`, `flag_class`, `risk_class`, plus four fixed
questions (destructive? irreversible or external? severity? would the owner's approvals cover it?). Free text fields
(`tool`, `resource_scope`) are forwarded only when short (at most 120) and made of identifier characters; otherwise
they are replaced by `[redacted]`. For email verbs a scope holding addresses is replaced by `[recipients]`.
Phase 3 sends no owner context at all.

Never sent: the command, the `display` text, argv or its digest, the target, hints, worker or tenant ids, credentials.
The key file path and key never appear in audit, records, the store snapshot, MCP output or owner routes (the status
route shows the endpoint **host** and the key **fingerprint** only).

A resolved model id that differs from the configured one (a concrete checkpoint behind `laya-auto`) is expected: it is
recorded on the shadow record and is not an error.

### Fail-closed table

| Situation | Result |
|---|---|
| No active grant, or the judge version changed | `pending`, source `owner` (a stale or empty judge version on an active grant shows as grant state `shadow`) |
| Active grant, global shadow period still running | `pending`, source `shadow` |
| Active grant, `backend_confirmed` not `true` (even if the judge would answer) | `pending`, source `judge_unavailable` |
| Active grant, judge answers and does not escalate | `authorized`, source `grant` |
| Active grant, judge escalates | `pending`, source `judge` |
| Active grant, judge unreachable, timed out, malformed, 401/429/5xx, disabled or no runtime | `pending`, source `judge_unavailable` |
| Grant revoked, expired or re-versioned **while the judge was being consulted** | re-checked after the consult: `pending`, source `owner` |

### Shadow records and ground-truth rules

Operational limits to know about:

- The recorder consults with a **fixed 30 s timeout** (not `timeout_seconds`).
- The recorder queue lives in memory and is **lost on restart**: a request that is pending across a restart gets no
  `JudgeRecord`, and its later owner label is dropped (there is no record to label). Jobs dropped from a full queue
  are not resubmitted.
- A slow consult on the authorize path occupies one hub handler thread for up to `timeout_seconds` (max 60) per
  authorize attempt.

For every **new** `pending` request with a known key, the hub queues a job on a single background recorder. The
recorder is **async** (never on the request path), **bounded** (100 jobs) and **drop-oldest** (`recorder.dropped` in
the status counts the losses). A `JudgeRecord` holds ids, verb, risk class, judge version, model and the verdict
numbers; status `ok` or `unavailable` (with a short reason code). It never holds command or display text.

Labels come from the owner's decision on the request:

- The **first** owner label wins. A later change of mind is not a new label: revoking an **active** grant marks the
  *approved* records for that key and worker as **regret**, which counts like a denial. Revoking a proposed, shadow or
  expired grant marks nothing (it never authorized anything).
- Records are grouped per (tenant, user, worker, key id) when reports are built, and a group is **one** label, so
  `min_labeled_decisions` counts distinct (worker, action) pairs, not requests. Negative wins inside a group: if any
  record in it was denied or regretted, the group counts as a negative label (and as a false negative when the judge
  called that record safe), so a later denial is never hidden by an earlier approval.
- Only records with status `ok` made under the **current judge version** count. Unavailable records never do.
- A false negative is a judge "safe" verdict (no escalation) on an action the owner denied or regretted. An approval is
  not proof of safety, so the bound is an estimate: owners misclick and approve things they later regret.

### The gate and its refusal codes

`activate` computes a calibration report per risk class from the owner's labeled records. All of these answer **409**
with `code`, in this order (as in `policy.activate_grant` and `calibration.gate_satisfied`):

| Code | Meaning |
|---|---|
| `judge_disabled` | No judge runtime, or `judge.enabled` is false. |
| `backend_unconfirmed` | `backend_confirmed` is not `true`. |
| `stale_report` | The report's judge version differs from the live one. |
| `insufficient_labels` | Fewer than `min_labeled_decisions` (default 200) labeled decisions in the grant's risk class. |
| `insufficient_diversity` | Labels come from fewer than 5 distinct action keys. |
| `no_discrimination` | Fewer than 5 denied or regretted labels, so "always safe" cannot pass. |
| `fn_bound_exceeded` | The one-sided 95% Clopper-Pearson upper bound on the false-negative rate is not below `max_false_negative_bound`. |
| `judge_error` | The gate could not be evaluated (treated as a refusal). |

Because the default needs 200 labeled decisions per risk class, no gate can pass soon after a fresh start.
Activation stamps the grant with the judge version.

### Judge version resets

`judge_version` is a hash of the `model`, the normalized `endpoint` (scheme, host, port and path; spelling and a default port
do not matter), the `thresholds` and the question-set version. Repointing the endpoint means a different backend, so it resets trust like the rest. Changing any of them makes
every report and every grant stale: active grants show as `shadow` and nothing is authorized until a fresh set of
labels exists under the new version and the owner activates again. Key file and timeout do not change it. Once evidence is ready again, a **new proposal** replaces a grant that became
`shadow` because of a judge-version change; activate it (gate permitting) to re-earn autonomy.

### Owner status and checks

- `GET /api/v1/assistant-judges` / `herdr-eng control-plane assistant-judge status --api-key-file PATH`: enabled, model,
  version, `backend_confirmed`, endpoint host, key fingerprint, recorder queue and drops, per-class calibration and the
  gate result (`satisfied`, `code`) per risk class.
- `POST /api/v1/assistant-judges/check` / `assistant-judge check`: one synthetic consult with a harmless fixed key. It
  records nothing and touches no grant.
- Both are operator-only and rate limited (429 `rate_limited`).
- Workers with the `read` scope get the MCP tool `judge_status`: enabled flag, mode (`shadow`), version, labeled counts
  and a boolean gate per class. No endpoint, fingerprint or per-request data.

### Not verified yet (read before relying on this)

- **Which backend serves production.** Whether `llm.metabolomics.us` serves real Laya or the mock cannot be told from a
  response. Use `GET /v1/systemone/runtimes` or ask the operator, then set `backend_confirmed`.
- **Model ids on the Go gateway** may be canonical checkpoint ids rather than `laya-auto`. `register` then fails with
  `model_not_listed` and prints the ids it saw; pick one and set `model` to it.
- **Latency.** Real Laya is measured at about 1.3 states per second per CPU node, which can exceed the 5 s default
  timeout; every timeout is a fail-closed `pending`. Tune `timeout_seconds` after `assistant-judge check`.
- **The integration tier has not been run against a real System One.** The documented response shape used in tests is
  the documented sample, not a recorded real response.
- **Determinism of real Laya** across calls is unmeasured; calibration assumes it is stable enough.
- **Deferred:** the canary prompt (a known-bad probe to catch a mock or degraded judge). The other Phase 3 deferrals
  (hook-attested evidence, harness adapters, the owner digest) are still deferred; see Phase 4, "Not verified".

### Integration-test environment variables

`@pytest.mark.integration` tests that talk to a real System One (a mock backend is fine) are skipped unless both are set:
`HERDR_JEV_TEST_URL` (base URL) and `HERDR_JEV_TEST_KEY_FILE` (path of a key file); `HERDR_JEV_TEST_MODEL` is optional
(default `laya-auto`). Run: `python3 -m pytest tests/control_plane/test_assistant_hub_jev.py tests/control_plane/test_assistant_hub_phase3_e2e.py -m integration`.
New audit action names: none beyond those above; consults and authorizations carry the judge version and the four answers
as percentages, never text.

## Phase 4: Slack front door, deploy permission and outcome check

Phase 4 puts the owner's decisions in Slack and lets the owner switch on automatic deploys for one project and one scope.
Everything in this section is **off by default**: with `personal_assistant.slack.enabled: false` (the default) and no
`personal_assistant.deploy` block, the hub behaves as in Phase 3. Design: `docs/superpowers/specs/2026-11-04-assistant-hub-phase4-design.md`.

### Slack app setup and manifest

Create one Slack app in your workspace (Socket Mode, so the tailnet-only hub needs no public endpoint). A manifest that
matches what the code calls (`auth.test`, `apps.connections.open`, `conversations.open`, `chat.postMessage`, `chat.update`):

```yaml
display_information:
  name: herdr assistant
features:
  bot_user:
    display_name: herdr assistant
    always_online: false
oauth_config:
  scopes:
    bot:
      - chat:write        # chat.postMessage / chat.update
      - im:write          # conversations.open: the owner's DM
      - im:history        # the owner's threaded replies in the DM (answers to questions)
settings:
  event_subscriptions:
    bot_events:
      - message.im        # thread replies in the DM; add message.channels / message.groups plus channels:history /
                          # groups:history only if you set personal_assistant.slack.channel to a channel id
  interactivity:
    is_enabled: true      # the Approve / Deny / Approve and remember / Confirm / Cancel buttons
  socket_mode_enabled: true
```

Then generate an **app-level token** with the `connections:write` scope (it starts with `xapp-`) and install the app to
get the **bot token** (`xoxb-`). Find your own Slack member id (profile, "Copy member ID", like `U01ABC23DEF`).

### Registering the tokens (CLI only)

```
herdr-eng control-plane assistant-slack register --bot-token-file /etc/herdr/slack-bot.token \
  --app-token-file /etc/herdr/slack-app.token --key-stdin
```

- The bot token then the app token are read from **stdin** (one per line, or two hidden prompts on a terminal); there is no
  argv or environment path, and nothing prints a token. The command proves the bot token with `auth.test` and the app token
  with `apps.connections.open`, writes both files with mode 0600 (`PATH.new` then rename), and prints fingerprints, team
  and bot user id only. `--base-url` defaults to `https://slack.com/api` (`http` only for loopback).
- `assistant-slack status --bot-token-file ... --app-token-file ...` is local only: it reads file metadata and calls nobody.
- Failures print `assistant-slack register failed: <code>`; exit codes 0 ok, 1 refused, 2 usage.
- The command does **not** edit the YAML. Add the block below yourself, including your `owner_user_id`.

```yaml
personal_assistant:
  slack:
    enabled: true
    base_url: https://slack.com/api
    bot_token_file: /etc/herdr/slack-bot.token
    app_token_file: /etc/herdr/slack-app.token
    owner_user_id: U01ABC23DEF        # required when enabled; the ONLY Slack account whose taps and replies count
    channel: ""                       # a channel id (C/G/D...), or empty for the owner's DM
    ask_ttl_minutes: 15               # 1..120; an unanswered ask then stays pending
    max_asks_per_task_per_hour: 6     # 1..1000; further asks are batched into one line
    summary_idle_minutes: 30          # 1..1440; a task with no final report gets a summary after this much quiet
    max_asks_per_worker_per_hour: 20  # 1..1000; per worker across ALL its task ids (task ids are worker-controlled)
    max_asks_per_hour: 60             # 1..10000; across all workers; overflow folds into the batched line
```

`owner_user_id` comes from this YAML, never from worker registration. The Slack runtime serves one owner and every
principal visible to it: a tap or reply from `owner_user_id` is honoured for any tenant/user the hub holds asks for.
`grants.approval_ttl_minutes` (default 15, in the `grants` block) is how long a one-shot owner approval of an exact action
stays spendable before it lapses.

Inline tokens (`bot_token`, `app_token`, `token`, `secret`) are rejected at load, and the two files must differ.

### The widened trust boundary (read this)

A verified Slack tap is **owner evidence of provenance `owner_slack`, counted exactly like `owner_api`**. This widens the owner
trust boundary from "the operator API key" to "the operator API key **or** the Slack account named in `owner_user_id`,
authenticated by your Slack workspace". Anyone who controls that Slack account (or your Slack app tokens) can approve actions. Protect
both like the operator key, and use 2FA on the account. Accepted by the owner on 2026-11-04.

A tap counts only when all of these hold: it arrives over the authenticated Socket Mode connection; it comes from
`owner_user_id`; it is on the very message the hub posted (same channel and `ts`); it carries the ask id plus its one-time
nonce; the ask is still open and unexpired; and the request is still pending. Re-posting, editing or forwarding a message
approves nothing, a second tap is refused, and a tap on a request decided elsewhere closes the ask and removes the buttons.
Refused taps write `assistant.slack.refused` with a reason. A worker is never told about the Slack channel.

### One-shot approvals, approve-and-remember, questions, summaries

- **Ask.** A new pending `action_request` (and only that; vetoes post nothing) posts one message with **Approve**, **Deny**
  and **Approve and remember**. The worker's text is made inert (mentions, links and markup neutralized) and never carries
  secrets. The worker learns the answer by **asking again**: an approved request returns `authorized` once (source `owner`)
  and the approval is spent; the next identical request is a new pending one.
- **Approve and remember.** The tap approves, records the evidence and then tries to activate the resulting grant through the
  *unchanged* gates (global shadow period, evidence thresholds and diversity, the judge gate). If a gate refuses, the approval
  stays recorded, the thread says in plain words what is still missing, and the wish is kept and retried at the next
  qualifying `owner_slack` approval for the same worker, key, tenant and user. Nothing is relaxed for ordinary actions.
- **Questions.** `assistant_ask` with a `question` posts a message and returns `{"status": "pending", "ask_id": ...}`; the worker
  polls with `assistant_ask {"ask_id": ...}`. Only a text reply **in that message's thread from `owner_user_id`** answers it
  (`answered` plus the text, returned as untrusted data). A reply is never an approval: a reply on an action ask is refused
  with `reason=not_question`. A question the hub could not post yet reports `pending` and is posted when Slack is back;
  past its ttl it is `expired` (`reason: not_asked` if it was never shown). Another worker, tenant or user sees `ask not found`.
- **Rate limit.** `max_asks_per_task_per_hour` bounds posts per worker and task; `max_asks_per_worker_per_hour` and
  `max_asks_per_hour` bound them per worker (whatever task ids it invents) and globally; the rest are folded into one batched line and
  posted as the window frees. A burst of questions does not delay an action ask on the same task.
- **Per-task summaries.** When a worker's `report` for a `task_id` is marked `final: true` (or the task has been quiet for
  `summary_idle_minutes`), the hub posts **one** summary per worker and task: counts (authorized under grants, one-shot
  approvals used, denied, vetoed, each worded "at least N"), any deploy outcomes of that task, and the worker's inert report text.
  Quiet work sends nothing else.

### Deploy permission per project and scope

```yaml
personal_assistant:
  deploy:
    precondition_timeout_seconds: 10    # 1..60: the overall deadline for the hub's checks per request
    deploy_check_minutes: 20            # 1..240: how long the hub watches for the new version after authorizing
    deploy_window_minutes: 10           # 1..240, not above deploy_check_minutes: before this a different version only
                                        # means "not deployed yet"; after it, a different version is "NOT live"
    projects:
      herdr-engineering:                # project id: [a-z0-9][a-z0-9_-]{0,63}
        repo: berlinguyinca/herdr-engineering
        scopes:
          control-plane:                # scope id, same pattern
            mode: ask                   # ask (default) | auto
            command: deploy/control-plane/deploy.sh      # relative path: no leading / or ./, no .., no spaces
            require: [on_main, ci_green, descends_from_deployed]   # non-empty subset; default all three
            health_url: http://127.0.0.1:8080/api/v1/health         # required if require is explicit and names descends_from_deployed
```

(`deploy.projects.<p>.scopes.<s>` takes only `mode`, `command`, `require`, `health_url`; unknown or secret-looking keys are
rejected at load. `health_url` is `https`, or `http` for loopback only, with no userinfo, query or fragment.)

- **Matching is the hub's call, never the worker's.** A request maps to a project and scope only when it is exactly
  `<command> --ref <40 lowercase hex chars>` and the worker's `repo` hint agrees with `repo`. Anything else is an ordinary
  unknown action, and the hub never says `authorized` for it on the strength of the deploy config.
- **`mode: auto` is only a candidate until you confirm it.** `POST /api/v1/assistant-deploys/<project>/<scope>/auto`
  (operator key, body `{"worker_id": ..., "confirm": true}`) *asks*: it posts a Slack message with **Confirm** and **Cancel**
  and returns 202 `awaiting_confirmation`. Only the verified Confirm tap creates the standing grant (source `owner_standing`,
  confirmed by `slack:<owner>`), through the same judge gate and shadow period; if a gate refuses, the thread says what is missing and
  nothing is created. `DELETE` the same path with `?worker_id=...` revokes it. `GET /api/v1/assistant-deploys` lists configured
  scopes and live standing grants.
- **`mode` is the owner's kill switch, read from the hub's live config on every request** (not cached per grant or per
  request). There is no hot-reload path for the assistant config: edit the YAML and restart the control plane. Once the hub runs with `mode: ask`, every
  deploy for that scope is `pending` with source `deploy_mode`; the standing grant stays stored but is ignored until
  `mode` is `auto` again. Removing the scope from the config has the same effect.
- **Standing permission is not a bypass.** It replaces only the approval count and session/task diversity for the deploy
  scope. Vetoes, the global shadow period, `effective_state` (judge version, policy version, expiry) and the judge all still apply, and the
  judge can still escalate a deploy to you.
- **Hub-verified preconditions.** Each name in `require` is checked by the hub itself, with the worker's claims ignored
  (`hint` and `environment` text change nothing): `on_main` (the sha is the tip of the default branch, via the `github.app` GitHub
  App), `ci_green` (GitHub status and check runs for the commit **and** the latest Woodpecker push pipeline for that commit on
  the default branch, via Woodpecker's unauthenticated read endpoints), and `descends_from_deployed` (the sha is strictly ahead
  of the commit `health_url` reports as deployed; the same sha, a prefix or non-hex data is not a new deploy). A failed check
  is `pending` with `failed <name>`; a check that could not be completed (timeout, error, odd data, no client, no
  `health_url`) is `pending` with `unchecked <name>`; both source `deploy_preconditions`, and the owner is asked. Nothing
  defaults to allow. A one-shot owner approval of the exact request skips the preconditions, because you decided it.

### After a deploy: the hub's own outcome check

When a deploy is authorized the hub stores a **deploy check** (it survives a hub restart, which a control-plane redeploy causes) and
polls `<health_url>?probe=1` itself; the worker's word is never used for "healthy". Statuses and the exact Slack wording:

| Status | Meaning | Slack |
|---|---|---|
| `awaiting` | still polling; an unreachable endpoint is expected while the container is replaced | nothing yet; summaries say "still being checked" |
| `healthy` | **provisional**: the check stays `awaiting` while the target is seen, and finalizes only once the target has been seen serving for `deploy_window_minutes` counted from its FIRST sighting (stored in the check, so a restart or poll gap does not reset it; a version that first appears late gets a full window, capped by `deploy_check_minutes`) (a prefix of at least 12 characters, or a newer commit that contains it). The hub runs inside the web process `deploy.sh` recreates, so it sees its own new version at once; a later worker-reported nonzero `deploy_exit` overrides even `healthy` | `Deploy healthy` (with a note if main moved or could not be re-checked) |
| `rolled_back` | after `deploy_window_minutes` (or once the target was seen and then replaced) a different version is serving; or the worker reported `{"deploy_exit": 1}` | `Deploy not live: ... is not serving. A different version is serving (...); the new version is not live; cause unknown.` The hub does **not** claim a rollback happened or that the old version "is back" |
| `unconfirmed` | `deploy_check_minutes` ran out without a confirmed result, or there is no `health_url` | `Deploy unconfirmed ... Do not assume it worked` |
| `failed_rollback` | the worker reported `{"deploy_exit": 3}` in an `outcome` report, at any time, even after `healthy` (matched by `task_id`, else the worker's most recent check). With no hub check at all, the hub still posts `URGENT: ... reported deploy.sh exit 3 ..., but the hub has no deploy check for it` | `URGENT: the deploy ... failed to roll back`, never batched or rate-limited |

Exit 0 never upgrades any status. Exit 1 posts a `rolled back` notice.

A deploy whose lineage cannot be verified is never reported healthy; a check that could not even be opened is posted as
`Deploy NOT verified by the hub`. The `rolled_back` status name is historical: it means "a different version is serving", nothing more.

### Limits you must know

- **The hub is advisory, as in Phase 2.** A worker asks voluntarily. The hub cannot stop a harness from running a command it
  was not authorized for; it guarantees only that it never answers `authorized` for it and that it checks the outcome
  afterwards. Enforcement at the harness (hooks) is deferred.
- **Worker working directory and repository are not attested.** The `repo` in a request is the worker's own hint, and the
  hub cannot see which checkout or working directory the worker then runs `deploy.sh` from. A hint can only make matching
  stricter (a mismatch leaves the request unmatched); it cannot widen it. What the hub does bind is the exact argv and the 40-character
  sha, which it verifies against GitHub, Woodpecker and the health endpoint. Run deploy workers only from the intended checkout.
- **Deploy exit 3 and the worker's report.** When `deploy.sh` exits 3 (rollback failed) the hub is usually down, so the
  worker's `report` may itself fail. A failed report on deploy exit 3 must be relayed to the user directly, by the worker.
  An out-of-process notifier is deferred.
- **Scope-gated deploys.** For a repo configured in `deploy.projects`, the generic `deploy` tool (`deploy.prod` /
  `deploy.canary`) is never grantable and never one-shot: it is always `pending` and points to the scope `/auto` flow.
  "Approve and remember" on a sha-bound deploy ask is refused with the same pointer.
- Slack taps widen the owner boundary as described above; the Socket Mode link has no per-request signing secret, so the
  app-level token plus the user id and nonce checks are the whole authentication.

### Not verified yet (Phase 4; read before relying on this)

- **Nothing has been run against a real Slack workspace, real GitHub, real Woodpecker or a real herdr-engineering deploy.**
  The tests use real sockets and real HTTP servers on 127.0.0.1 that speak the documented Slack Web API, Socket Mode and
  GitHub / Woodpecker / health shapes; they are not recordings of the real services, and Slack's real payloads, rate limits
  and reconnect behaviour may differ.
- **`default_clients()` wiring is untested against real services.** The production construction of the GitHub App client,
  the Woodpecker provider and the health client from host config has not been exercised against GitHub, Woodpecker or a real
  health endpoint; the tests build equivalent clients pointed at local listeners.
- **Socket Mode against production Slack** (reconnects, `disconnect` envelopes, app-level token rotation) is unexercised.
- The `rolled_back` / `unconfirmed` / `failed_rollback` paths are tested against local listeners that change their answers, not
  against a deploy that really rolls back.
- **Deferred:** blue/green deploys, hook-attested evidence, Codex/OpenCode/Pi harness adapters, the canary prompt, the owner
  digest, and harness-level enforcement.

## Testing notes

The end-to-end flows are `tests/control_plane/test_assistant_hub_e2e.py` (Phase 1), `test_assistant_hub_phase2_e2e.py` (Phase 2), `test_assistant_hub_phase3_e2e.py` (Phase 3, judge via a raw-socket listener serving the documented shape) and `test_assistant_hub_phase4_e2e.py` (Phase 4: a real Socket Mode WebSocket listener plus real GitHub, Woodpecker and health listeners; run it with `-W error`), each with a real control plane and hub on 127.0.0.1, port 0.
Tests use a throwaway `HOME`; never point a test at your real `~/.claude`, `~/.codex` or `~/.config/herdr`.
`@pytest.mark.integration` smoke runs the real `claude mcp add` line (skipped when `claude` is not on PATH):
`python3 -m pytest tests/control_plane/test_assistant_hub_e2e.py -m integration`.

Sandbox note (pre-existing): every `/api/v1` route imports `asyncpg`, so on a machine without it those tests fail with
500. Install it into a separate directory and put that on `PYTHONPATH` (for example
`pip install --target /path/to/deps asyncpg` then `PYTHONPATH=/path/to/deps python3 -m pytest ...`).
