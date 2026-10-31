# Prototype: InferWeave gateway behind the fabric LLM endpoint

Status: prototype, opt-in (`--profile llm-gateway`). The python `llm-proxy` stays the default.

## What it proves
Project keys minted by the control plane (`hk-…`, stored only as SHA-256) can be the
credentials of a real InferWeave gateway, so agents reach more than `/v1` chat:
System One (`POST /v1/systemone`) and whatever the gateway serves next, with the
gateway's own admission, queueing and per-principal priority.

`control-plane llm-gateway-sync` renders each active key as the principal
`herdr:<project>:<key_id>` with value `sha256:<digest>` in
`auth.client_principals_file`. No plaintext key is written beside the gateway.

Measured locally with the real gateway under the supervisor (shared client token on):

| call | result |
| --- | --- |
| System One, project key | 503 (past auth; no runtime registered in the test) |
| System One / chat, bogus key | 401 |
| chat, project key | 404 (past auth; no model) |
| worker register, project key | 401 (client keys never open worker routes) |
| new key after mint | accepted within ~2 s |
| revoked key | 401 after the restart |

## Needs three inferweave changes (branch `feat/principals-prehashed`)
1. `principals.rs` accepts `sha256:<64 hex>` values (malformed is an error).
2. `/v1/systemone` accepts `client_principals_file` keys. It accepted only worker-grade
   `principals_file` keys, which a project key must not be.
3. The gateway follows `client_principals_file` while running (polled every 2 s, atomic table
   swap). An unreadable file, a parse error or a name shared with a worker principal empties
   the table: deny all, never the last good one. Measured: a new key worked and a revoked one
   was refused within ~2 s, with the gateway started once.

## Layout
- `llm-gateway-sync` container: renders keys, loops, needs no Rust binary.
- `llm-gateway` container: the gateway only, waits for the principals file.
- `llm-proxy` stays: chat, token accounting, custom models. One `hk-` key works on both;
  sessions get `INFERWEAVE_BASE_URL` when `llm.inferweave_url` is set.

## Not done, and why
- **Peering to llm.metabolomics.us.** Nothing persistent runs yet to peer, and it needs a
  gateway-kind enrollment code on the production gateway plus a preserved identity (as on
  Whiteale). Do it when the gateway runs on the fabric host.
- **Token accounting from the gateway.** Its outbox holds raw prompt records and no per-request
  token counts, so tailing it would copy prompts into the dashboard. Needs a usage feed in the
  gateway. Until then llm-proxy remains the accounting path.
- **Custom models** stay on llm-proxy for the same reason.
- **Image.** `llm-gateway/Dockerfile` is ready but must be built from a reviewed Linux
  release of a merged commit; none exists for these inferweave changes yet.
- **Bring-up.** The compose file is covered by the render tests only; the containers were not
  started (no reviewed binary).
