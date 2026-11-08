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
    ref: v1.0.0                                              # required: a TAG or a commit sha (7-40 hex); no default
```

The pin is immutable: a **tag or a commit sha**. A branch name is refused at install time ("is a branch; pin a tag or a
commit sha"); a name that is both a tag and a branch resolves as the tag (`refs/tags/`). Unknown keys, URLs with userinfo
and unsafe refs (leading `-`, `..`, `//`, `@{`, `/.`, trailing `/`, `.` or `.lock`, over 128 chars, whitespace) are refused,
so no secret can be stored or echoed. The same ref rules are enforced in Python (`parse_agent_rules`) and in the script
(`agent-rules-sync.sh --validate-ref`); a test keeps them identical. Read-only copy for tooling:
`GET /api/v1/agent-rules/config` (same `X-API-Key` rules as other `/api/v1` reads; `404 {"error": "not configured"}` when
unset or invalid; no write route).

## Distribution

`ansible/tasks/agent-rules.yml` (included by `fleet-sync.yml` and `site.yml`) runs `scripts/agent-rules-sync.sh <url> <ref>
~/.agent-rules`:

1. clone on first run, otherwise fetch (tags included) and refresh the remote URL. Git runs non-interactively over ssh:
   `GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"` (a fresh host has no github.com
   `known_hosts` entry; an operator-set `GIT_SSH_COMMAND` wins);
2. resolve `ref` as a tag, then a commit sha; **fails the play if it is missing or only a branch** and leaves the checkout
   untouched;
3. detached checkout, then `./install.sh` (idempotent; run every time);
4. prints `rules=<short sha> changed=<bool> install_changed=<bool>`. The task reports `changed` when HEAD moved or the
   installer changed anything (harness symlinks into the checkout, `~/.claude/CLAUDE.md`).

A failure does not abort the rest of the convergence: the task's `rescue` records the reason and the fingerprint reports
`rules=FAILED:<reason>`; `fleet update` then reports the hosts under `steps.agent_rules.failed_hosts` and is not `ok`.

`herdr-eng fleet join` and `fleet update` read `control_plane.agent_rules` from `--fleet-config` (default
`~/.config/herdr-engineering/fleet/fleet-config.yaml`, also with `--no-credentials`) and pass `agent_rules_repo_url` /
`agent_rules_ref` as extra vars. No config file or no section means no pin: a notice is printed and the task is skipped.
A config file that exists but is unreadable, or a section that is invalid, stops `join` (provisioning is skipped) and
`update` (before anything is pulled or deployed) with a non-zero exit and a message naming the file. Hosts need git access
to the repo (deploy key or ssh agent) for ssh URLs.

## Parity

`fleet-fingerprint.yml` records the short sha of `~/.agent-rules` (`MISSING` when absent, `FAILED:<reason>` after a failed
sync). `fleet status` and the `fleet update` parity listing print `rules=<value>` per host, and the fleet-sync / fleet-status
fingerprint line carries it. Rules are shown there only: they are not a `fleet drift` component (that matrix is built from
tool versions the hosts report to the control plane, and nothing reports the rules revision there yet), and they are
not part of the revision parity gate except that a `FAILED` host makes `fleet update` not ok.

## Follow-up

`ai-sync-enforce` (the unit `scripts/herdr-eng-ai-sync-enforce.service` execs `herdr_engineering.ai_sync_enforce --env claude`) is
hardcoded to `claude`, so ai-sync-managed config for the other harnesses (`codex`, `opencode`, `pi`, ...) is not force-synced on
enforced hosts even though `control_plane.ai_sync.envs` can list them. Make it honor `ai_sync.envs`. Not done here.
