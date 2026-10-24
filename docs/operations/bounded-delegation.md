# Bounded task delegation and artifact proposals (T11)

A room owner can hand a coworker's agent (an external agent, see `external-agents.md`) ONE bounded task. The agent answers
with an *artifact proposal*. The proposal waits in quarantine until a room owner accepts or rejects it. Accepting records it in
the lineage graph with provenance. **Nothing is ever applied, merged, run or written to a workspace by this feature.**
It extends the existing HCN service: the task store, the append-only event log, the external-agent credentials, MCP and A2A.
There is no second service, store or credential system. Federation between servers is not implied.

## Switch (default OFF)
```yaml
# fleet-config.yaml
control_plane:
  collab:
    delegation_enabled: true     # default false; YAML only (no environment variable, no command-line flag)
```
It is re-read on every request (T16 M15): editing it takes effect without a restart, and a missing, unreadable or non-boolean file means `false`. It is a `control_plane.collab` key, so it works for both the combined server and `web --collab-only` (and for
`fleet config render-collab`). While it is `false` no delegation can be created, answered, proposed to or accepted; the MCP
tools and the A2A skill/`tasks/*` methods are not advertised (A2A answers "no task state" as before). Rejecting a proposal,
cancelling a task and reading what exists stay possible so an owner can clean up. Flipping it back to `false` is the kill switch:
quarantined proposals stay where they are and cannot be accepted until it is `true` again.

A delegation also needs a lineage graph in the same process (accepting writes to it). The isolated `--collab-only` process has
none, so there `POST /rooms/{room}/delegations` answers 409 `graph_unavailable` rather than strand proposals.

## Flow
1. Invite the coworker's agent with `proposals: true` (Tabs > "Invite a remote agent" > "be given bounded tasks", or
   `POST /api/v1/collab/rooms/{room}/externals/invites {"label": ..., "proposals": true}`). It still gets only `read`/`post`, no
   `execute`, no terminal. Without `proposals` it can never be a delegation target.
2. The owner delegates (Proposals tab, or `POST /rooms/{room}/delegations`):
   ```json
   {"target_agent_id": "agent_...", "text": "Why does the parser crash on empty input?",
    "scope": {"paths": ["src/parser/"], "nodes": ["commit:abc123"], "questions": [{"id": "q1", "text": "why does it crash?"}]},
    "budget": {"max_proposals": 2, "max_steps": 20, "max_artifact_bytes": 16384, "max_tokens": 100000, "max_turns": 60, "max_hops": 1},
    "expires_in_seconds": 3600, "output_kinds": ["analysis", "patch"]}
   ```
   * `scope.paths`: repository-relative files, or a directory ending in `/`. No `..`, absolute or drive paths, backslashes, `.git*`, `.husky`
     or `.github/workflows`. `scope.nodes`: graph node ids. `scope.questions`: `{id, text}`. At least one; at most 20/20/10.
     A patch needs `paths`; an analysis needs `questions` or `nodes`. The scope grants **no read access** to anything: it is the
     list a proposal's references and patch paths are checked against.
   * `budget`: `max_proposals` 1-5, `max_steps` 1-200 (progress calls + proposals), `max_artifact_bytes` 256-32768, `max_tokens`
     (see limitations: advisory), `max_turns` 1-200 (default 60: every answer the delegate sends, i.e. accept, progress and each
     proposal are the non-final turns; the next one closes the task `failed`, 409 `budget_exhausted`, while `complete` (the result), `decline`, `fail` and `cancel` are always allowed), `max_hops` 1-3
     (default 1: the ceiling on delegation-chain depth; the delegate is hop 1 and an external agent has no route to delegate onward, so 1 is the
     only depth reached today; it is echoed so a future onward path has the bound already). A task stored before `max_turns` existed has no turn ceiling (it is not failed on replay). `expires_in_seconds` 60-86400, never past the delegate's credential expiry.
   * `output_kinds` is a non-empty subset of `patch`, `doc`, `analysis`, `link`.
   * Only room owners delegate; at most 3 open delegations per agent, 20 per owner per 10 minutes; `idempotency_key` supported.
3. The delegate (REST, MCP or A2A, same service methods) reads its inbox, answers `accept` | `decline`, may send `progress`, and
   submits proposals. Every state-changing call carries `expected_version` (the task's current `version`); a stale version is a 409.
   Finally `complete`, `fail` or `cancel`.
4. The owner reviews in the Proposals tab or `GET /rooms/{room}/proposals[/{id}]` and decides with
   `POST /rooms/{room}/proposals/{id}/decide {"decision": "accept"|"reject", "expected_digest": "<digest shown>", "reason"?}`.

## What a proposal is
`kind` is `patch` (a strictly parsed unified diff), `doc` or `analysis` (plain text), or `link` (an https URL kept as a string and
never fetched by the server). Server-side checks, identical on every protocol:
* size cap (the task's `max_artifact_bytes`, at most 32 KiB), text only: NUL, control, bidirectional, zero-width and other invisible
  characters, any `\r` (so CRLF text is refused with code `crlf`; nothing is converted, send LF), and secret-like strings are **refused**, not repaired (a repaired patch is not the patch the reviewer read);
* patch: every `diff --git`, `---`, `+++` and hunk header is parsed and counted; refused: paths outside `scope.paths`, `..`/absolute/backslash/
  drive/`:` paths, a hard-coded deny-list of files a checkout executes (`.git*`, `.husky`, `.githooks`, `.github/workflows/`, `.github/actions/`,
  `.gitlab-ci.yml`, `.circleci/`, `Jenkinsfile`, `.pre-commit-config.yaml`, NTFS 8.3 short names such as `GIT~1`; no room owner or delegate can lift it, and there is no
  operator override either), `---`/`+++` names with any whitespace or invisible character (refused, never stripped), `/dev/null` headers that disagree with the hunk
  ranges (creation needs one `-0,0` hunk, deletion one `+0,0` hunk, as `git apply` reads them), renames and copies, any mode change, new file modes other than `100644`
  (so no symlinks `120000`, submodules `160000` or executables `100755`), `GIT binary patch`/`Binary files`, more than 20 files or 2000 lines;
* link: https only, no credentials in the URL, port 443, DNS name with a dot (no IP literals, `localhost`, `.local`, `.internal`, `.lan`);
* `refs.paths/nodes/questions` must be inside the scope;
* the proposal id and `digest` are a SHA-256 over the task, kind, title, content and refs, so identical content is the same proposal
  (a replay costs nothing) and the digest the owner echoes binds the decision to exactly what was reviewed.

States: `quarantined` -> `accepted` | `rejected` | `withdrawn` (by the delegate) | `superseded` (by a newer proposal of the same
delegate, via `supersedes`) | `expired` (undecided after 3 days). Every decision is terminal. Repeating the same decision is a
`replayed: true` no-op (a repeated *accept* also finishes a graph write that failed the first time); the opposite decision is a 409.

## What accepting does
* Needs the current room owner, the digest of what was shown, a proposal still in quarantine, a delegate that is not revoked or
  removed from the room, and a task that was not cancelled, declined or failed. A credential that merely expired, or a delegation
  whose time ran out after the proposal was made in budget, does not block acceptance.
* Writes `proposal_decided` to the HCN log, then records in the graph (room scope, written by the service, principal kind `collab`):
  node `task:hcn-delegation.<task id>`, node `artifact:hcn-proposal.<proposal id>` (attrs: kind, digest, bytes, delegate agent and label,
  delegating owner, accepting owner and time, `trust: "accepted-untrusted-content"`, `applied: false`, `executable: false`; never the
  content) and a `produced` edge from the task to the artifact. Event ids and sequences derive from the proposal id, so a retry is a duplicate.
* Those two id prefixes are reserved: only the HCN service (and an authenticated operator key) may write a node or edge touching them;
  ordinary ingest keys and the delegate itself are refused (`403 lineage_forbidden`). As defence in depth (not covered by a test), an API key whose configured role is spelled `external` or
  `collab` gets principal kind `unknown` rather than those service-only kinds.
* Applying a patch is a separate human action outside HCN. An applier must treat the content as hostile: `git apply --check` in a throwaway
  worktree, never through symlinks, never with hooks, after re-reading the diff.

## Rate limits and quotas
Delegate: 6 proposals per 10 minutes, 60 answers per minute, 120 inbox polls per minute, 30 proposals lifetime per credential. Sponsor:
150 proposals lifetime across its delegates. Room: 40 proposals in quarantine at once. Owner: 60 decisions per minute. Task: `max_proposals`,
`max_steps`, `max_turns`. Overrunning `max_steps`, `max_turns` or the reported-token budget closes the task (`failed`, 409 `budget_exhausted`, audited).

## Audit
Every create, answer, proposal, withdrawal, decision and promotion is an HCN log event with `actor_kind`, visible in the project audit
(`GET /audit`, content never included). Refusals (out of scope, hidden characters, stale version, quota, revoked delegate, digest mismatch,
budget) are audited `denied` with a reason, at most once a minute per reason.

## Protocol mapping (same checks everywhere)
MCP `tools/list` is per agent (T16): an external agent is never offered the task tools it is always refused (`tasks_inbox`, `tasks_wait`,
`task_update`, `task_create`, `agents_list`), sees the delegation tools only when it was enrolled with `proposals`, and an internal agent is
never offered the external-only delegation and consultation tools. Calling a tool that is not listed is refused exactly as before.

| Action | REST (`/api/v1/collab`) | MCP tool | A2A |
|---|---|---|---|
| inbox / status | `GET /delegations/inbox`, `GET /delegations/{id}` | `delegations_inbox` | `tasks/list` (extension), `tasks/get` |
| answer | `POST /delegations/{id}/respond` | `delegation_respond` | `message/send` + `taskId` + one `data` part `{action, expected_version, ...}`; `tasks/cancel` |
| propose | `POST /delegations/{id}/proposals` | `proposal_submit` | `message/send` + `taskId` + `data` part `{action: "propose", ...}` |
| withdraw | `POST /proposals/{id}/withdraw` | `proposal_withdraw` | n/a (use REST/MCP) |
| delegate, review, decide | REST, a person's token only | not offered | not offered |

## Rollout and rollback
Performance and exposure notes: the cheap checks (version, task state, budget, quotas, exact resend) run before the content is parsed, and parsing happens outside the
service lock; every pattern in the validator is bounded. `GET /rooms/{r}/tasks` shows a delegation's `bounds` and `version` only to room owners (the delegate has its own inbox).
The open ingest listener (no API keys configured) is not an operator for the reserved `task:hcn-delegation.*` / `artifact:hcn-proposal.*` ids.

Code only; new log events (`proposal_submitted`, `proposal_decided`, `proposal_promoted`, `task_progress`, and `bounds` on `task_created`)
are additive and ignored by older code, which would show a bounded task as an ordinary pending task addressed to an external agent that cannot
see it. No migration. Roll back by setting `delegation_enabled: false` (immediate), or by reverting the PR. Graph nodes already written stay.

## Known limitations
* Token budgets are declarative: the server counts proposals, steps, bytes and wall-clock time, and trusts the agent's `tokens_used` report.
* The delegate is an external agent (a human coworker uses their agent/bridge). Internal agents with `execute` use ordinary tasks; a signed-in
  person is not a delegation target.
* The server cannot know what is on a workspace's disk: a patch is validated as text and paths, not against files or symlinks that exist there.
* Prompt-injection detection is a hint for the reviewer only (`flags`); the control is that content is stored and rendered as inert data, is
  never added to any model prompt by this service, and is never applied.
* The accepted artifact's content stays in the HCN log (not in the graph); the log is not pruned by this feature. Quotas bound growth.
* The `agent:<delegate>` graph node belongs to the delegate (T10), so the `produced` edge starts at the delegation task node.
* Not a security certification; T16's independent review is still required.
