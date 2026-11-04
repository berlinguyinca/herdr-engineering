# herdr-memory harness installation

`herdr-eng memory` installs a small, engineering-scoped `herdr-memory` surface into
Pi, Claude Code, Codex and OpenCode through the existing CLI/installer. It is the
P02 phase of the unified Hindsight memory spec. It writes **no credentials**.

```sh
herdr-eng memory plan [--harness H ...]                 # diff, read-only
herdr-eng memory install --all [--dry-run] [--home DIR] # or --harness claude --harness codex
herdr-eng memory status | doctor | capabilities
herdr-eng memory repair | upgrade                       # re-apply missing/drifted managed blocks
herdr-eng memory uninstall [--dry-run]                  # managed blocks only
scripts/install.sh --with-memory                        # opt-in step 7b (or HERDR_INSTALL_MEMORY=1)
```

`--home` points every path at an isolated HOME (tests, canary review). Add `--json`
for stable machine output. Exit code is 1 on conflicts/invalid config/unhealthy doctor.

## Safety rules

- Every managed block carries `herdr-memory:managed`. JSON entries carry it in an
  `env`/`environment` value or hook command; TOML uses `begin`/`end` comment lines;
  skill files carry it in the first lines.
- Writes are atomic (temp file + `os.replace`, original mode kept) and preceded by a
  content-addressed backup in `~/.local/share/herdr-engineering/memory/backups/` (0600).
- Second run is a no-op: nothing is rewritten, no new backup, manifest unchanged.
- User config and foreign entries are preserved. If `herdr-memory` already exists in a
  harness config (or skill path) without the marker, the unit is reported as `conflict`,
  left untouched, and the command exits 1. Invalid JSON/TOML is reported, never rewritten.
- Harnesses whose binary is not on PATH are skipped.
- Uninstall removes only marked blocks (and files this installer created), then the
  manifest. Foreign entries survive. Roll back by restoring the backups if needed.
- The adapter never writes Codex hook-trust state.

## Verified capabilities per harness (this host, 2026-11-02)

| Harness | Version | Managed surface | Lifecycle | Fallback |
|---|---|---|---|---|
| claude | 2.1.308 | `~/.claude.json` `mcpServers.herdr-memory` (as `claude mcp add -s user`); `~/.claude/settings.json` hooks `SessionStart`, `UserPromptSubmit` (type `command`); skill | Native: bounded recall injected as labelled untrusted data | none for recall; retain explicit only |
| codex | 0.166.0 | `~/.codex/config.toml` `[mcp_servers.herdr-memory]` (as `codex mcp add`); skill | **Not guaranteed.** Hooks exist but are gated by hook trust; not installed | MCP tools + CLI skill, agent must call explicitly |
| opencode | 1.18.23 | `~/.config/opencode/opencode.json` `mcp.herdr-memory` (type `local`); skill | **None.** Plugins exist but no verified lifecycle contract | MCP tools + CLI skill |
| pi | 1.4.0 | skill only (`~/.pi/agent/skills`) | **None** here; Pi's lifecycle belongs to the pi-engineering `DurableMemoryProvider` seam. `pi mcp` has no verified add surface | CLI skill |

`herdr-eng memory capabilities` prints the same table as JSON, with each harness's
detected version. Nothing retains automatically in any harness (secrets/transcripts
must not be persisted); retain is an explicit MCP/CLI action.

## MCP surface and scope

Adapter 3 configures Codex's `memory_recall` tool with
`approval_mode = "approve"` and `memory_retain` with `approval_mode = "prompt"`.
This permits the installed recall surface in noninteractive sessions without
changing global approval policy, other MCP servers, or hook trust. Retention
still follows Codex approval and the explicit/evidence gate. The CLI may retry
previously accepted outbox operations while reading; the adapter does not label
the tool read-only to hide that behavior. Codex documents these per-tool settings
in its [MCP configuration reference](https://learn.chatgpt.com/docs/extend/mcp).

`herdr-eng memory mcp-serve --harness H` is a stdio MCP server with `memory_recall` and
`memory_retain`. It shells out to the `herdr-memory` CLI (the authoritative client), holds
no credentials and passes none in argv. Scopes are limited to `project`, `global`,
`infra`, `mission:<id>`; anything else (including personal-assistant hub scopes) is
rejected before any call. Retain requires `explicit=true` or evidence refs. Recall output
is untrusted data. `herdr-eng memory hook` (Claude hooks) exits 0 and prints nothing on
any failure, so memory trouble never blocks a session. The server is only a client: it
needs `herdr-memory` on PATH and a project config, and depends on the P01 contract
(`recall --scope`, `--max-results`, `--timeout-ms`; `retain` scopes) being deployed.

## Pin

`lock/upstreams.yaml` pins `herdr-memory` at `cc20ad4d46845b8257e54596e152846e3ce4a3c2` (client `0.1.6`, contract v1, private
repo); the same value is `memory_install.PINNED_REVISION` and a test keeps them equal.
`memory_install.PINNED_CLIENT_VERSION` is the release `herdr-memory --version` is expected to
report. The installed manifest records the pin used. `scripts/install.sh --with-memory`
installs the harness adapters only; the herdr-memory package itself must be installed at the
pinned revision - bumping the pin changes the managed-block marker, so every host needs
`herdr-eng memory upgrade` afterwards.

## Doctor

`herdr-eng memory doctor` (also folded into `herdr-eng doctor` once a managed install
exists) reports: per-unit config validity and drift; manifest pin vs current pin;
installed `herdr-memory` revision vs pin - read from the install that **owns the CLI**
(`<prefix>/lib/python*/site-packages/herdr_memory-*.dist-info/direct_url.json`), because
the wrapper's interpreter usually cannot import the client; `client_version` vs
`PINNED_CLIENT_VERSION`; the last unattended controller update (`fleet_update`, see
[`fleet-lifecycle.md`](fleet-lifecycle.md)); credential files named in
`~/.config/herdr-memory/config.json` (`projects.*.credentials`, `scopes.*.credentials`)
must be mode 0600 in a 0700 parent (contents are never read); and server health through
`herdr-memory status`. Health is `unknown` when the CLI is absent, unreachable, or does not
report `healthy` - it is never assumed healthy.

Because `herdr-memory status` resolves its project from the current directory, `doctor`
resolves one to ask with (`default_project`, else the only configured project) and passes
`--project`; and because the client defaults to `codex` - a harness a project may have no
key for, which the client reports as `unreachable` - it also selects a harness the project
has a credential for (a harness `doctor` is managing, else any configured credential) and
passes `--harness`. The check reports what it probed and how both were chosen (`project`,
`project_source`, `harness`, `harness_source`). A project with no credentials at all yields
`unknown - cannot probe without a credential`, never a FAIL that blames the server. When neither is resolvable it says so
(`no project to report on: no default_project and 2 projects configured; ...`) instead of
reporting `UNKNOWN server` as if the server were broken
([herdr-engineering#571](https://github.com/berlinguyinca/herdr-engineering/issues/571)).
Memory itself is unreachable from unmapped directories unless `default_project` is set in
the client config - see `herdr-memory` `docs/client.md`.

## Known limits

- Claude/OpenCode config files are edited directly (not via the harness CLI) so changes
  are diffable and testable; a concurrently running Claude may rewrite `~/.claude.json`.
  Re-run `memory status` after sessions end.
- Reformatting: JSON files are rewritten with their detected indent; key order is kept.
- OpenCode `opencode.jsonc` is not handled.
- Fleet rollout is out of scope; use plan/doctor on a canary first (MIGRATION_ROLLOUT.md).
