# Shared agent rules

Every fleet host gets the shared rules repo (`git@github.com:berlinguyinca/agent-rules.git`) cloned to `~/.agent-rules`,
checked out at one pinned ref, and installed with its `install.sh` (links `AGENTS.md` into the harness directories and
ensures the `@import` line in `~/.claude/CLAUDE.md`).

## Configure the pin

Central fleet YAML, `control_plane.agent_rules` (validated at load; a bad section keeps the last good file):

```yaml
control_plane:
  agent_rules:
    repo_url: git@github.com:berlinguyinca/agent-rules.git   # https:// or ssh git URL, no credentials
    ref: v1.0.0                                              # required: tag, branch or commit; there is no default
```

Unknown keys, URLs with userinfo and unsafe refs (leading `-`, `..`, whitespace, `.lock`) are refused, so no secret can be
stored or echoed. Read-only copy for tooling: `GET /api/v1/agent-rules/config` (same `X-API-Key` rules as other
`/api/v1` reads; `404 {"error": "not configured"}` when unset or invalid; no write route).

## Distribution

`ansible/tasks/agent-rules.yml` (included by `fleet-sync.yml` and `site.yml`) runs `scripts/agent-rules-sync.sh <url> <ref>
~/.agent-rules`:

1. clone on first run, otherwise fetch (tags included) and refresh the remote URL;
2. resolve `ref` (tag, then `origin/<branch>`, then commit); **fails the play if the ref does not exist** and leaves the
   checkout untouched;
3. detached checkout of the resolved commit, then `./install.sh` (idempotent; run every time);
4. prints `rules=<short sha> changed=<true|false>`; the task is `changed` only when HEAD moved.

`herdr-eng fleet join` and `fleet update` read `control_plane.agent_rules` from
`~/.config/herdr-engineering/fleet/fleet-config.yaml` and pass `agent_rules_repo_url` / `agent_rules_ref` as extra vars.
Without a pin (no section, no `agent_rules_ref` in the inventory) the task is skipped. Hosts need git access to the repo
(deploy key or ssh agent) for ssh URLs.

## Parity

`fleet-fingerprint.yml` records `fp_rules` (short sha of `~/.agent-rules`, `MISSING` when absent). `fleet status` prints
`rules=<sha>` per host and `fleet-sync` / `fleet-status` include it in the fingerprint line. A `rules` tool in the host
inventory is drift-compared like any other component and `herdr-eng fleet drift` suggests `herdr-eng fleet update --host
<host>` for a host that differs or is missing it. Rules are informational in `fleet update` parity (not a gate).

## Follow-up

`ai-sync-enforce` (the unit `scripts/herdr-eng-ai-sync-enforce.service` execs `herdr_engineering.ai_sync_enforce --env claude`) is
hardcoded to `claude`, so ai-sync-managed config for the other harnesses (`codex`, `opencode`, `pi`, ...) is not force-synced on
enforced hosts even though `control_plane.ai_sync.envs` can list them. Make it honor `ai_sync.envs`. Not done here.
