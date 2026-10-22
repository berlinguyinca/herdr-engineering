# Agent helpers: agents start their own helpers (design)

Status: approved by the operator 2026-10-22 ("please implement this"); items marked *my call* were settled by the designer, not stated by the operator. Repo: herdr-engineering (collab service, host runner, dashboard). Brainstormed 2026-10-22.

## Intent
An agent in a collaboration room can start helpers on its own, with no approval click, for four reasons:
1. **Context relief**: hand a side job to a fresh helper so the agent's own context stays clean.
2. **Handoff**: write a handoff and start a successor when the agent is running out of room.
3. **Second opinion**: another model reviews or debugs the agent's code.
4. **Fan-out**: the agent starts sub-agents, they work, and the results come back to it (the superpowers sub-agent workflow).
Also: a helper with a capability the parent lacks (for example vision).

This applies to **anyone's agents**, including coworkers' agents that reach the system through the public site. That is deliberate (the operator's decision). Safety therefore comes from limits, a sandbox and an egress allowlist, not from an approval click on each spawn.

## Goals
- Helpers have tools. A helper never has more permissions than its parent in that room.
- Per-room **profile** set by the room owner: API keys, environment variables, SSH keys, allowed tools, allowed models, a spend ceiling, and which external hosts helpers may reach.
- **Network egress is blocked by default**; the operator approves hosts; approvals expire by default and can be promoted to a permanent per-room list.
- A **decision step** picks the cheapest model that can do each task (no strong model to read a file).
- Helpers appear under their parent's herdr tab and in the flow graph (already built: `parent_agent_id`, `herd-<prefix>` layout).

## Non-goals
- Not a general remote-execution API: an agent never chooses the command that runs.
- No per-spawn human approval (explicitly declined). Host access is the one approval that stays.
- Not changing how pi-engineering missions run (its herdr runtime has no production caller; separate project).
- No secrets in GitHub, in task text or in logs.

## Anti-goals (a failure even if it works)
- A prompt-injected helper leaks a room secret to an arbitrary host.
- A coworker's agent grows a tree of helpers that exhausts bender or runs up model spend.
- A helper gains access its parent did not have.

## What the agent can do
A new MCP tool `helper_spawn` (and `helper_wait`, `helper_result`, `helper_cancel`) on the existing collab MCP endpoint. Inputs: `role` from a fixed list (`reviewer`, `debugger`, `worker`, `vision`, `successor`), `task` text, optional `family` (claude-code, codex, pi) and optional `model` hint, optional `wait` flag. It cannot pass a command, path, environment or secret. Task text is untrusted content and is passed on stdin, never to a shell (as workers do today).

## Limits (defaults; room owner may lower, operator may change)
- 3 live helpers per agent; nesting depth 2; 20 spawns per person per hour; global ceiling on concurrent helpers per host.
- A helper expires after a time limit (default 2 hours) and its panes close.
- Revoking an agent revokes its whole subtree. A dashboard **panic switch** stops all automatic spawns.
- Every spawn, refusal and host request is logged in the audit log and shown as a room event.

## Room profile
Owner-defined per room; stored on the host that runs the helpers (a 0600 file under the runner's state directory), never in the repo and never in the event log. Holds: secret references (API keys, env vars, SSH keys), allowed tools, allowed families/models, spend ceiling, permanent host list. A helper receives at most its parent's profile slice, injected into its sandbox at start. Tightening or revoking a profile applies to every helper in the room.

## Egress and host approval
- Helpers run in the bubblewrap sandbox with **no direct network**; traffic goes through a runner-controlled proxy (allowlist by host and port). This is a change from today, where the sandbox leaves the network open.
- A request to an unlisted host is blocked and recorded; the helper gets a plain "access pending" answer and waits.
- The operator sees room, agent, host and port on the dashboard and approves or denies. Approval is **per room** (all agents in the room can then use it).
- Approvals **expire** (default 24 hours, my call) and can be promoted to a permanent list per room. The dashboard lists every external host each room has tried, with remove buttons.
- The harness's own model endpoint must stay reachable; it is on the permanent list by construction.

## Model choice (decision step)
Runs in the host runner (Python). Truth source: the gateway model catalogue (no hard-coded model ids). Each task gets a tier (trivial, standard, hard, vision); **rules first** (role, task size, mentions of images or tools; my call), with a cheap-model classifier a later addition. Scoring borrows pi-engineering's `RoleRouter` ideas (cost, latency, capability, local preference) and its rule that a reviewer is never the same model as the author. An agent's `family` or `model` hint is honored only inside the room's allowed models and spend ceiling. Each choice is logged with its reason.

## Results and handoff
- Parent calls `helper_wait` (long-poll like `tasks_wait`) or polls `helper_result`; the result is the helper's final output, size-limited, delivered as untrusted text.
- `successor` role: the parent writes a handoff note (stored as a room artifact) and the helper starts with it; the parent then ends. Same limits apply.

## Where it runs
On the same host as the parent, placed by the existing runner, as a pane split from the parent's tab (layout already shipped). The runner polls spawn requests as it does today.

## Constraints
- Public site stays an isolated container with no DB/AWS credentials; the spawn tool is reachable through the existing authenticated MCP path only.
- Flag-gated, existing endpoints unchanged, spec's security requirements remain release blockers; an external security review is still pending.
- Dashboard private (tailnet), as today.

## Decisions recorded after the builder check
- **Spawn permission** (*my call*): `spawn_helpers` is one more action in the existing per-room agent grants. The room owner ticks it per agent; without it `helper_spawn` is refused. Room profile secrets belong to the owner and are injected only into helpers of agents holding it. The hourly rate limit is per person.
- **Secrets** (*my call*): API keys are injected by the proxy for the host they belong to; the helper never holds them. SSH keys are injected into the sandbox as files (they cannot be proxied), so a room with an SSH key should keep its host list tight.
- **Spend ceiling is optional** (operator): off unless the owner sets it. When set (*my call*): tokens per room per day, measured at the gateway.
- **Permission slice** (*my call*): room profile limited to what the parent's grants allow; approved hosts are room-wide (operator).
- **Network** (*my call*): fixed permanent list of each family's model endpoint; proxy resolves DNS and tunnels any allowed host:port.
- **Identity** (*my call*): the runner mints each helper's own credential at spawn; the server records the parent link; helpers may spawn within the depth limit.
- **Limits** (*my call*): over a limit, spawn fails with a clear message (no queue); the panic switch blocks new spawns and stops running helpers.
- **Files** (*my call*): fresh working directory per helper; reviewers/debuggers get the parent's directory read-only; workers return output or a patch.
- **Successor** (*my call*): replaces its parent at the same depth; parent ends; chain capped at 5.
- **Host approval wait** (*my call*): counts toward the helper's time limit; a denial is remembered for one hour.

## Order of work
1. Room profile + egress proxy + host approval (everything else relies on it).
2. `helper_spawn/wait/result/cancel`, limits, subtree revoke, panic switch, audit.
3. Decision step (rules), then optional classifier.
4. Dashboard: profile editor, host requests, spawn tree.

## Left to the builder
Proxy implementation (for example a small HTTP CONNECT proxy on a Unix socket bound into the sandbox), profile file format, exact role prompts, UI layout, test strategy.
