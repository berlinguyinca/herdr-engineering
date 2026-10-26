# Phase 8 — failover, leases, fencing, and safe rebalancing
No automatic live rehoming without proven per-harness resume contract and safe checkpoint boundary. New work is redirected first. Existing running work remains on host unless it can be drained/checkpointed. A host going offline enters `suspect`, then `reconciling`; do not start competing writer until original lease is fenced or ownership is definitively released. Prefer persistent generation fencing token checked by workspace write guard and scheduler admission, with durable authoritative store. Process/shell disconnection ≠ agent stopped. Reconcile after scheduler/container restart, SSH timeout and network partition. Maintain one active execution lease per agent with idempotency and monotonic generations.

## Migration transaction
1. Operator/automatic policy selects checkpoint-capable agent, capacity-reserved destination; mark `migration_requested`.
2. Wait for safe quiescence (no editor/build/commit/tool command in flight), checkpoint consistent session & workspace state to protected store with digest, stop origin under supervision, revoke/fence old lease.
3. Validate destination tool and workspace manifest, transfer/checksum private checkpoint, restore with higher epoch/generation and reconstruct terminal/session pointer.
4. Verify new heartbeat and Git/workspace consistency; mark success, release old reservation; on failure maintain safe stopped state or restore origin only after definitive lease ownership, with visible audit.
5. Unsupported agent/harness remains pinned or `draining`; never kill-and-restart pretending continuity.

## Fault matrix
Partition after commit-before-ack, heartbeat lost while still running, slow SSH, 2 schedulers, expired lease while tool runs, full RustFS, corrupt checkpoint, stale auth, shutdown mid-file transfer, concurrently dirty Git worktree, mismatched Pi/Claude resume version, unavailable safe fence. Every case must ensure no duplicate execution and no loss of silently accepted work. For external unsandboxed agents, explain why robust write fencing may require stricter execution scopes; do not claim safety where enforcement is impossible.
