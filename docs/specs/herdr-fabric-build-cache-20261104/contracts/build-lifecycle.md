# Build lifecycle, locking and failure semantics
States: requested -> classified -> admitted -> running -> publishing -> completed OR failed/cancelled/timed_out; rejected is terminal. Every transition persisted with unique attempt ID and reason. A cache HIT is not a completed build unless matching full output/evidence validated. Do not treat compiler object hit as build success.

Idempotent request key = scoped repository + source/input digest + validated toolchain/profile/flags + command kind + trust domain. Dirty trees require deterministic snapshot digest or are excluded from full-build dedup. Use bounded TTL and explicit invalidation; active same-key requests can single-flight but receive independently correlated results.

Leases have owner, epoch/fencing token, TTL, heartbeat, scope and expiration. Never delete active targets or artifacts with valid leases. On controller restart reconcile live processes, Slurm jobs and stale leases; never assume detached jobs died. Build maintenance exclusive lock must remain compatible with existing per-host `flock` usage. Avoid TOCTOU paths in deletion.

Failure: cache endpoint unavailable => local compile. Slurm unavailable => explicit local fallback if configured, else actionable failure. Storage checksum mismatch => quarantine entry, alert, recompile. Cancellation => terminate only owned process/job, wait for final state, preserve diagnostics. Eviction => dry-run first, bounded batch, recheck lease under lock.
