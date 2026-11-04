# Pi host runner v1

The operator-side runner attests one digest-pinned Pi artifact and the
`task-restricted-v1` profile. Owner-visible host summaries expose only host ID, freshness,
supported runtime/profile names, and approved model IDs. `ready` means a fresh unused host
attestation; it does not mean an agent process exists.

The control plane supplies a package digest, selected approved model, fixed limits, agent
credential and one-time claim token. It never supplies commands, paths, environment variables,
mounts or requested actions. The runner constructs a fixed no-tools Pi argv, private `models.json`
pointing at the host egress broker, a private worker credential file, and a named strict sandbox
scope. The agent is reported ready only after the worker successfully polls its MCP task inbox.

Cancellation terminates the worker process group, stops the activation's named systemd scope and
checks that the scope is inactive before acknowledging cancellation. Checkpoints use monotonic
sequences and carry only bounded phases and output hashes.

## Bender evidence

An isolated live smoke under `/tmp/herdr-pi-runtime.sjCBCs` used the private broker service and
strict sandbox with 512 MiB memory, 64 processes, 100% CPU and a 120-second wall limit. The staged
Pi 1.4.0 artifact digest was
`sha256:b92a3cfc23ddd22af3c2d9a1520ab9e5596cf5887c8929e4d57158769d3f080f`.
Pi exited successfully with no stderr and resolved
`qwen3.8-flash-next-unsloth-ud-iq4_xs`; reported usage was 624 input, 1358 output and 1982 total
tokens. The redacted receipt is `pi-proof.json` in that scratch directory. No tool, operator-home,
Docker socket or direct-network access was granted.

The live smoke proves kernel sandbox plus brokered model execution. The registered-agent lifecycle
was subsequently proven through the ephemeral control-plane integration below; package registration
and host attestation by themselves remain non-authoritative definitions.

## Registered lifecycle evidence

The ephemeral owner/operator control plane registered package
`package_1a9ffa34fa043d70cad717b0eee6498831d00930bfae811f726a42749b173e64`
against the same artifact and exact 1 GiB / 128 process / 100% CPU / 120 second profile.
Runtime `collab_01m68bz8v500000h0emjamyg5m` reached ready only after its worker successfully
polled MCP. Task `grant_01m68c154x00000n0perz0qzb1` completed in the strict sandbox with exit
zero, no workspace changes, and the owner-visible result `HERDR_REGISTERED_PI_OK\n`. The package
selected the approved alias `qwen3.8-flash-modality-text-quant-iq4_xs`; the earlier direct provider
receipt resolved that alias to `qwen3.8-flash-next-unsloth-ud-iq4_xs`.

Owner cancellation immediately removed execution access. The controller stopped the worker and
verified its named systemd scope inactive before reporting runtime state `cancelled`, checkpoint
sequence 11 / phase `finished`, with no remaining lease. Private host evidence remains under
`/tmp/herdr-pi-runtime.sjCBCs`: `preflight-proof.json`, `pi-proof.json`, and the activation's
hash-chained `audit/tasks.log`. These files contain hashes and runtime evidence; credentials are
kept in separate private activation state and are not part of this document.
