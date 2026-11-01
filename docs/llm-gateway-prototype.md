# Prototype: InferWeave gateway behind the fabric LLM endpoint

> **Direction (2026-11-01): the shared entrypoint is the embedded relay, see the next section.**
> The gateway profile below stays as the way to run a gateway of our own; the relay profile is
> how the fabric reaches the InferWeave network.

## The shared entrypoint: an embedded inferweave relay (`--profile llm-relay`)
The dashboard and every fabric member send llm, embedding, System One, speech and any other `/v1/*`
request to ONE `inferweave relay` with their `hk-` project key. The relay holds the upstream
credential, forwards to the gateway in `llm-relay/relay.yaml`, and counts tokens per key. It is the
same binary as the gateway image, so there is no new codebase and no second image.

```
agents / dashboard --hk- key--> relay (herdr-llm-relay) --upstream credential--> gateway --> nodes
        control plane: mints/revokes keys (llm-gateway-sync writes the digests the relay follows)
                       reads usage        (llm-relay-usage polls /api/usage)
```
Requires inferweave with the shared-entrypoint relay (InferWeave/inferweave#1441): `principals_file`,
`admin_secret_file`, `GET /api/usage`, streamed-usage counting.

Measured with the real relay binary against a stub upstream and the real herdr sync and usage
commands: chat (JSON and streamed, 21 in / 8 out from the final `usage` event), embeddings, System
One and binary speech all pass through; the upstream sees only the relay's credential, never the
caller's key (0 occurrences); no key, a bad key and a revoked key are 401 (revocation within the 2 s
poll); `/api/usage` and `/metrics` are 401 without the operator secret; usage reached the control
plane's analytics per project and model.

Deploy notes: `HERDR_LLM_GATEWAY_DIR` holds `secrets/admin` (operator secret) and
`secrets/upstream-token` (the credential the relay presents upstream; for llm.metabolomics.us an
access key created in the console). On the gateway's own host the public name does not resolve from a
container (hairpin NAT): add `extra_hosts: ["llm.metabolomics.us:host-gateway"]` to `llm-relay` in an
override file. Token counts cover chat and embeddings; System One and speech count requests per
model but the upstream reports no tokens for them. Custom models stay on `llm-proxy`.

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

## Token accounting (done)
The gateway counts provider-reported tokens per caller principal and model and serves them at
`GET /v1/admin/usage` (admin secret; counts only, no prompt text). `control-plane
llm-gateway-usage` polls it and emits one `token_usage` event per request (`source=llm-gateway`)
for `herdr:<project>:<key_id>`. The baseline is saved only after the control plane accepted the
batch; a gateway restart is detected by `startedAt` and counted from zero.

## Measured in a Linux container (arm64 build of the gateway commit, mock node)
| step | result |
| --- | --- |
| chat with project key | 200; bogus key 401; usage route without admin secret 401 |
| 4 + 2 requests, report | control plane shows 6 requests, 66 in / 30 out (11/5 each) |
| report again, no traffic | unchanged (no double count) |
| gateway restarted, 2 requests | +2 exactly, never negative |
| key minted and synced | accepted with no gateway restart |
| key revoked and synced | 401, principals file `{}` |

Not measured: `--adaptive` (the compose command) serves no static nodes without a controller or
peer, so the proof ran without it. Real traffic arrives through peering.

## Layout
- `llm-gateway-sync` container: renders keys, loops, needs no Rust binary.
- `llm-gateway` container: the gateway only, waits for the principals file.
- `llm-proxy` stays: chat, token accounting, custom models. One `hk-` key works on both;
  sessions get `INFERWEAVE_BASE_URL` when `llm.inferweave_url` is set.

## Bring-up on an amd64 host (zapp, 2026-11-01)
Ran as an isolated compose project (`-p herdrgw`, loopback ports 18480 web and 8089 gateway)
beside the production gateway, with image `herdr-llm-gateway` built from the verified
`node-ci-latest` binary (commit `5d60af17`; `/health` reports that revision). What it took:
- `docker build` without BuildKit: drop `--chmod` from the `COPY` and `chmod 755` the binary first.
- `secrets/kek` is **64 hex characters** (`openssl rand -hex 32`); anything else is refused.
- `nodes.json` is an object, `{"nodes":[]}` for an edge with no static nodes, not `[]`.
- `control-plane migrate` once before the web service answers (the worker normally does it).
- The sync and usage services now run as `1000:1000`: the gateway image runs as that user and
  could not read a root-owned 0600 `principals.json`.
Checked live: a minted `hk-` key is accepted by the gateway within a poll interval (404 for an
unknown model, not 401), a wrong key gets 401, `/v1/admin/usage` is 401 without the admin
secret, and a revoked key returns 401 and leaves `principals.json` within a poll interval.

## Not done, and why
- **Peering to llm.metabolomics.us.** Needs a gateway-kind enrollment code minted on the
  production gateway and a preserved identity (as on Whiteale), and something persistent running
  to peer. Do it when the gateway runs on the fabric host.
- **Custom models stay on llm-proxy, by design.** They are project-scoped routes to external
  OpenAI-style URLs; the gateway is a fleet router and has no such proxy mode.
- **A reviewed image: the binary exists, the image is built where Linux amd64 is native.**
  The CI-gated Linux release `node-ci-latest` was published for merged commit `5d60af17`
  (`inferweave.commit`); its `inferweave` asset is x86-64 static-pie, 69706112 bytes, sha256
  `54254ff46b5a487cecfa5de0ae9999183cf55dd3dc0fd28b5c4c9da2f1af9b9e`, verified against
  `inferweave.sha256`, and it contains the pre-hashed principals and `/v1/admin/usage` routes.
  Build the image from that asset with `llm-gateway/Dockerfile` (needs BuildKit for `COPY
  --chmod`) on an amd64 host. It was not built or run on the arm64 Docker Desktop VM used for
  the prototype, so the amd64 binary itself has not been executed here.
- **Full compose bring-up** (control plane, postgres, `llm-proxy`): the gateway container and the
  sync and usage commands ran for real; the compose file as a whole is covered by render tests.
