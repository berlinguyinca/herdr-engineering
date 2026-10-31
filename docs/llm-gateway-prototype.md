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

## Needs two inferweave changes (on branch `feat/principals-prehashed`)
1. `principals.rs` accepts `sha256:<64 hex>` values (malformed is an error).
2. `/v1/systemone` accepts `client_principals_file` keys. It accepted only worker-grade
   `principals_file` keys, which a project key must not be.

## Known limits
- The gateway reads client keys at startup only, so a changed key set restarts it
  (debounced, default 30 s; in-flight requests drain). With `llm.key_mode: session`
  that is a restart per agent session at worst. Live reload in the gateway is the fix.
- Not yet done: joining llm.metabolomics.us as a gateway-kind peer (needs a gateway
  enrollment code and a preserved identity, as on Whiteale), token accounting
  (`/api/v1/token-usage`) from the gateway's usage, custom models (stay on the python proxy),
  and a reviewed Linux binary for `llm-gateway/Dockerfile`.
