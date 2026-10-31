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
- Learning, grants driven by observed behaviour and the Jev judge are later phases. Nothing recorded now activates them.

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
3. **Register the server with Claude Code.** Run the printed line:
   `claude mcp add --transport http herdr-assistant http://HUB_HOST:9483/assistant/mcp --header "Authorization: Worker <credential>" --header "X-Herdr-Device: my-laptop"`
4. **Install the observe-only hooks (optional).**
   `herdr-eng control-plane assistant-worker install-hooks --harness claude --settings ~/.claude/settings.json`
   shows a diff and asks before writing (`--yes` skips the prompt; a timestamped backup is kept). It adds
   `PermissionRequest`, `PostToolUse` and `PostToolUseFailure` hooks that post what happened to
   `/assistant/hook-events`. The hook command is wrapped so it exits 0 with no output, never blocks a tool and never
   changes a permission decision. It is idempotent and only ever replaces its own entries.

`assistant-worker list` shows workers (no secrets). Only the `claude` harness has setup support in Phase 1.

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
- Rate limits per peer address: 120 requests/min for `/assistant/mcp` and `/assistant/hook-events` combined, 10/min for `/assistant/enroll` (429 beyond).
- At most 8 concurrent connections per peer; a request must finish within 15 s (slow clients are cut off).
- Request bodies over 64 KiB are refused (413).

## Testing notes

The end-to-end flow is `tests/control_plane/test_assistant_hub_e2e.py` (real control plane and hub on 127.0.0.1, port 0).
Tests use a throwaway `HOME`; never point a test at your real `~/.claude`, `~/.codex` or `~/.config/herdr`.
`@pytest.mark.integration` smoke runs the real `claude mcp add` line (skipped when `claude` is not on PATH):
`python3 -m pytest tests/control_plane/test_assistant_hub_e2e.py -m integration`.

Sandbox note (pre-existing): every `/api/v1` route imports `asyncpg`, so on a machine without it those tests fail with
500. Install it into a separate directory and put that on `PYTHONPATH` (for example
`pip install --target /path/to/deps asyncpg` then `PYTHONPATH=/path/to/deps python3 -m pytest ...`).
