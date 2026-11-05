# Evaluation: native sccache-dist as long-lived compiler workers (spec 12)

Status: evaluation only. **Decision: NOT implemented. No-go until the preconditions below are met.**
Nothing was installed, started, contacted or configured. Sources: `AUDIT.md` and the audit bundle
(`docs/specs/herdr-fabric-build-cache-20261104/audit/C-toolchain-storage-upstream.md`), read offline.
Every upstream statement not confirmed against an installed binary is marked UNVERIFIED.

## What upstream is said to offer
- sccache v0.19.0 (2026-10-24) is the latest release per the audit. UNVERIFIED locally: sccache is not installed on the audit host.
- Distributed compilation uses a scheduler plus build servers (`sccache-dist`). Per the upstream DistributedQuickstart (as summarised in audit C; UNVERIFIED offline): scheduler and build servers are Linux only (FreeBSD servers via a separate guide); build servers need bubblewrap >= 0.3.0 and run as root; non-linux64 clients need `icecc-create-env` or a prebuilt toolchain tar (`[[dist.toolchains]]`); `sccache --dist-status` reports state.
- Whether a Rust workload is distributable in practice (proc-macro, build scripts, bin/dylib crates, incremental) is UNVERIFIED. The audit already records that bin/dylib/cdylib/proc-macro crates are not cacheable by plain sccache and that build-script behaviour is unconfirmed.

## Constraints that matter for this fabric
1. **Linux-only workers.** macOS fabric hosts can be clients at best (UNVERIFIED: client toolchain packaging from macOS), never workers.
2. **bubblewrap and root.** A worker needs bubblewrap and root (per audit C). On a shared Slurm cluster ordinary users cannot be root and user namespaces may be disabled; whether a rootless or apptainer-wrapped worker works is UNVERIFIED. Installing bubblewrap or running anything as root is a host change needing explicit operator approval. We must not ask for or assume cluster admin rights.
3. **Auth tokens (per audit C, UNVERIFIED offline).** Scheduler `client_auth` (static token or Mozilla/LDAP), scheduler to server `server_auth` as `jwt_hs256` secret, server `scheduler_auth` `jwt_token`, client `dist.auth` token. That is three distinct secrets. Each must be referenced by absolute file path (mode 0600, owner-checked via `check_credentials_file`), never in config values, argv, env dumps, job comments or logs.
4. **TLS.** The scheduler should sit behind an HTTPS reverse proxy (per audit C). Plain HTTP between client and scheduler, or between scheduler and workers across hosts, is not acceptable; the existing `tls_required` rule (false only for `*.local-test` loopback) must apply. Certificate issuance and the proxy are new infrastructure and are out of scope.
5. **Network reachability.** Workers on cluster compute nodes must be reachable from the scheduler and clients. Which hosts can reach which is unverified (the memory notes already record one-directional blocks between Hive and the lab network). Do not assume an address or role for any host (for example Futurama is unverified).

## Why Slurm allocations should hold reusable workers
One Slurm job per crate is wrong: queue wait (minutes) and sandbox start dwarf compile time for most crates, scheduler load grows with crate count, and failures multiply. A worker should instead be a **single allocation that serves many compilations**:
- Lease: the allocation is bound to a build registry lease (`build_registry.Lease`, fencing epoch in job name and comment, as `build_slurm_adapter.render_name/render_comment` already do). A worker whose epoch is stale must stop accepting jobs.
- Heartbeat and idle teardown: the worker heartbeats through the registry; after an idle timeout or lease release the allocation ends itself. Cancellation targets only the exact job id whose comment matches the binding (`cancel_target`). No pattern-based `scancel`.
- Bounded wall time: the allocation time limit is shorter than the lease TTL horizon so an orphan cannot outlive its owner; a reconciler cancels leaked jobs by comment match.
- Fallback: if the scheduler is unreachable, the worker is absent or the lease is lost, builds run locally (`remote_fallback: local`), exactly as for whole-build Slurm.

## Credential scoping
- One secret set per trust domain; dedup never crosses trust domains (already true for request keys).
- Worker and scheduler secrets live only on the hosts that need them; a client holds only its client token.
- Short-lived where possible (rotate per allocation). Whether upstream supports per-allocation tokens is UNVERIFIED.
- Workers must not receive cache write credentials unless the cache policy allows writes from that trust domain.

## What must be verified on a real worker before any implementation
1. Exact sccache and `sccache-dist` version and the config keys (`scheduler.conf`, `server.conf`) against the installed binary.
2. bubblewrap availability and user-namespace behaviour on the target nodes, and whether a non-root worker is possible at all.
3. A real Rust workspace compiles remotely and the result is byte-for-byte equivalent in outputs we care about; which crate kinds fall back to local.
4. Toolchain packaging and the worker's rustc/target match the client's toolchain digest.
5. Latency benchmark: N compilations reuse one allocation; compare against local-only and against shared-cache-only builds. No savings may be claimed without this.
6. No orphan jobs after client crash, lease takeover, SIGTERM and walltime expiry.
7. TLS between every pair, token handling, and that no secret appears in `ps`, job comment, scheduler logs or builds.jsonl.
8. Failure semantics: scheduler down, worker lost mid-compile, network partition. Local build must still succeed.

## Decision
**NOT implemented.** Not a single line of worker, scheduler or sccache-dist code, config or deployment exists in the repo.

Preconditions (all required):
1. An operator-approved, verified Slurm runner and an approved test partition (spec 11 live work is blocked on the same).
2. sccache installed by the operator on the relevant hosts, version recorded.
3. Operator approval for bubblewrap and for any root or user-namespace requirement, or a verified rootless design.
4. TLS endpoint and secret files provisioned by the operator; no credentials created by an agent.
5. Reachability confirmed between client, scheduler and workers.
6. Items 1-8 above verified on a real worker with evidence.

**Go/no-go: NO-GO** today. Revisit when preconditions 1-5 are met; the self-contained adapter contract in `herdr_engineering/build_slurm_adapter.py` (lease binding, cancellation, output validation, fallback) is the only prepared piece.
