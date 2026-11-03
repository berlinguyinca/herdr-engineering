# `herdr-eng memory` adapter install + real harness sessions — 2026-11-03

Executed the remaining handoff items: install the adapters on two hosts, prove real
harness sessions, and pin a Pi runtime that carries #559. Canary project `canary`,
synthetic notes only, nothing secret reproduced here.

## D1 closed — Pi runtime pinned to a build containing #559

Real sessions load Pi's runtime from `~/.pi/agent/settings.json` → `packages`, which
pointed at `~/.local/share/herdr-engineering/pi/releases/4b89237…` — a build that has
no `src/blackhole/hindsight.ts` at all, so `PI_HINDSIGHT_ENABLED` did nothing and
`/remember` had no write path (that, not just an old runtime, is why D1 looked like a
retain failure).

- Backup taken first: `~/.pi/agent/settings.json.bak-d1-20261103T191741Z` (the previous
  `packages` value is also kept in `_d1_previous_packages`).
- Pinned to `~/.pi/pi-engineering/versions/0.2.0-06d0dbc3784f` — complete pi package
  (`pi-engineering-runtime` 0.2.0, `extensions/index.ts`, `node_modules`), and the fix
  is in it: `hindsight.ts:67 TIMEOUT_FLAG_ACTIONS`, `:72 HERDR_HARNESS … || "pi"`,
  `:211 --evidence` per ref.
- Fleet-wide equivalent of this step is `herdr-eng fleet rollout --pi-revision <40-char sha>`
  (`docs/operations/fleet-runtime-rollout.md`); the local pin is a single-host action.

Plain-session proof (`pi -nc -ns -np --mode json`, no `-e`, no
`PI_ENGINEERING_RUNTIME_SOURCE`, cwd mapped to `canary`) — the runtime itself, not the
model, made these CLI calls (wrapper-captured argv):

```
argv: [recall] [*] [--max-results] [50] [--scope] [project] [--timeout-ms] [5000]        # session_start
argv: [recall] [reply exactly] [--max-results] [20] [--scope] [project] [--timeout-ms] [5000]  # before_agent_start
argv: [retain] [<text>] [--scope] [project] [--provenance] [{…evidence_refs…}] [--evidence] [user-request:pi-note-…]
```

`/remember …` → `Saved private memory pi-note-c845d8cc-…`; server showed
`mem-87ebf153… rev 1 verified`, and both D1 canary notes were forgotten afterwards
(`remaining: 0`). `retain` now carries `--evidence` and never receives `--timeout-ms`.

## Adapter install — Mac

`herdr-eng` had to be upgraded first: the installed tool was an **editable install of
`~/IdeaProjects/herdr-engineering`** (branch `feat/llm-gateway-prototype`) which predates
`memory`. Reinstalled from an `origin/main` (`cd67484d`, #560) worktree with
`uv tool install --force /tmp/he-main`; to restore the previous setup:
`uv tool install --force --editable ~/IdeaProjects/herdr-engineering`.
Also linked the client onto PATH: `~/.local/bin/herdr-memory → ~/.venvs/herdr-memory/bin/herdr-memory`.

`herdr-eng memory install` created all eight targets, and a second `status` says every
target is `current` (idempotent):

```
noop pi skill · noop claude mcp · noop claude hooks · noop claude skill
noop codex mcp · noop codex skill · noop opencode mcp · noop opencode skill
```

Existing user configuration survived the merge:
`opencode.mcp = [context7, herdr-memory]`,
`claude mcpServers = [cclsp, context7, sequential-thinking, herdr-memory]`,
Claude hook events `PostToolUse, SessionStart, Stop, SubagentStart, UserPromptSubmit,
PreCompact` with SessionStart 4 entries / UserPromptSubmit 2.

Direct probes: hook runner output is labelled
(`<herdr-memory-recall untrusted-data="true">`), and one MCP `tools/call memory_recall`
roundtrip returned the seeded fact with `offline: false, degraded: false, timing_ms: 153`.

## Real harness sessions — Mac

| harness | session | evidence |
| --- | --- | --- |
| `pi` | pass | runtime-driven `recall`/`retain` above; `/remember` persisted |
| `codex` (`codex exec`) | pass | read the seeded fact (3.4 bar) **and** wrote `mem-79a31670…` through `memory_retain`, no shell used |
| `opencode` (`opencode run`, 1.18.23) | pass | `herdr-memory_memory_recall` → 3.4 bar; `herdr-memory_memory_retain` → `mem-c929a20a…` |
| `claude` (`claude -p`) | **blocked** | `You've hit your weekly limit · resets Nov 6 at 10am` — MCP/hooks proven only by direct probe |

## bender (host 2)

Non-login ssh PATH lacks `~/.local/bin`, so every probe below exports it explicitly.
Client config is the canonical schema (`url`, `cache_dir`, `projects.<name>.{tenant,
credentials.<harness> token files, paths}`) with all four harness credentials present;
`~/canary-harness-test` was added to `projects.canary.paths`.

**Blocker found and fixed:** the client already installed there
(`~/.local/share/uv/tools/herdr-memory`, reporting version **0.1.1**) was a *different
build* than the repo's 0.1.1 — its actions were `{status,sync,recall,remember,summary,forget}`
with no `retain`, no `--scope`, no `--max-results`, no `--evidence`:

```
herdr-memory: error: unrecognized arguments: --scope project --max-results 5
herdr-memory: error: argument action: invalid choice: 'retain'
```

so every MCP/hook/provider call there was failing. Installing the wheel built from
herdr-memory `b3c5074` (contract v1) fixed it:

```
status → {"project":"canary","harness":"codex","pending":0,"contract":1,"healthy":true,
          "outbox_pending":0,"dead_letter":0}
retain → {"state":"delivered","outcome":"persisted","document_id":"mem-c95cff3b…"}
hook claude SessionStart → <herdr-memory-recall untrusted-data="true"> … notes from BOTH hosts
doctor → OK credential:* (0600 in 0700), OK server healthy; overall: unknown
```

`herdr-eng` there also predates `memory`, so `origin/main` was installed into a **separate
venv** `~/.venvs/herdr-eng-main` (production venv `~/.venvs/herdr-engineering` untouched;
the inactive `herdr-eng-agent` unit still points at it). Managed blocks record that
absolute path, e.g. `command = "/home/wohlgemuth/.venvs/herdr-eng-main/bin/herdr-eng"`,
`args = ["memory","mcp-serve","--harness","codex"]`. Install created
`pi skill, claude mcp, claude hooks, claude skill, codex mcp, codex skill`, skipped
`opencode` (no binary), and the second `status` reports all `current`.

**Cross-host shared memory proven:** the note retained on bender (`mem-c95cff3b…`) is
returned by a recall run on the Mac, alongside the Mac harness-written notes.

Real harness sessions on bender are quota-blocked: `codex exec` →
`You've hit your usage limit … try again at Nov 4th, 2026 6:40 AM`; `claude -p` →
`weekly limit · resets 2am`.

## New defects

| id | sev | defect |
| --- | --- | --- |
| D7 | **P0** | A different `herdr-memory` build shipped on bender under the same version string `0.1.1`, without the contract-v1 verbs — every adapter call there failed, and nothing (not `--version`, not `doctor`) could tell the two builds apart. Deploy must install the pinned wheel by path/hash and `doctor` must probe the contract (`retain --help`, `status.contract`) instead of trusting a version string. |
| D8 | P1 | `memory mcp-serve` resolves the client with `shutil.which("herdr-memory")`; off a login shell that is empty and the server answers `{"error": "herdr-memory CLI not found", "offline": true}` while the hook prints nothing — silent fail-open. The installer pins an absolute `herdr-eng` path but leaves the client PATH-dependent; pin an absolute `HERDR_MEMORY_CLI` into the managed block env. |
| D9 | P1 | bender's production `herdr-eng` (`~/.venvs/herdr-engineering`) has no `memory` subcommand, so adapters had to be pointed at a side venv. Either the control-plane deploy installs a memory-capable build, or hosts keep a second venv that nothing maintains. |
| D10 | P2 | Project resolution needs cwd ∈ `paths`; `default_project` is not a fallback ("Current directory has no configured project"), so harness sessions in unmapped directories silently get no memory — and `doctor`'s server probe, run in cwd, then reports UNKNOWN. |
| D11 | P2 | `herdr-memory --version` is unsupported (usage error), so `doctor` can only ever say `UNKNOWN package_revision` — which is exactly how D7 hid. |
| D12 | info | MCP `memory_retain` with `explicit=true` stores no provenance (`provenance: {}` on the Mac notes vs the CLI's `{"source_system": …, "evidence_refs": […]}`), so harness-written facts lose origin. Related to D4 (harness dropped from normalized results). |

## Acceptance criteria

- **#2 (installed on both hosts, idempotent, no clobber)** — met: Mac 4/4 harnesses and
  bender 3/4 (`opencode` absent there) are `current`; re-running changes nothing; existing
  MCP servers and Claude hooks preserved.
- **#4 (two harnesses see the same memory on two hosts)** — met through Pi, Codex and
  OpenCode sessions on the Mac plus CLI/hook reads on bender and the cross-host recall of
  `mem-c95cff3b…`; **not** yet met for a *live Claude Code* or *bender Codex* session — both
  providers are quota-capped (Codex resumes 11-04 06:40 PT, Claude 11-06 10:00 PT).
- **#5 (failure drill)** — still not run; needs explicit authorization to perturb the
  production stack.

## State left on the hosts

- Mac: `settings.json` packages pinned as above (backup retained); `herdr-eng` runs from an
  `origin/main` worktree `/tmp/he-main` (reinstall as `--editable ~/IdeaProjects/herdr-engineering`
  to go back); canary credentials in `~/.config/herdr-memory/config.json`, whose `paths` now
  include `~/canary-harness-test` and `.worktrees/pi-harness-id`.
- bender: side venv `~/.venvs/herdr-eng-main`, contract-v1 client wheel in the uv tool env,
  `~/canary-harness-test` mapped to `canary`; production `herdr-engineering` venv and the
  inactive agent unit unchanged.
- Because the adapters point at **canary** credentials, `herdr-eng memory uninstall` (or
  replacing them with real project entries) is the cleanup step when the canary project is
  retired; blast radius is limited to the two mapped directories.
