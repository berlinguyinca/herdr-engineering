# HerdR availability, startup, and catalog performance

The user approved all three priorities: coordinated deployment, complete durable
projection checkpoints with incremental replay, and filtered/paged service catalogs.
Existing routes, identities, permissions, current/history policies, and legacy API
responses remain compatible. No new project dependencies or pickle serialization.

## Deployment coordination

`deploy/control-plane/deploy.sh` owns a host/user-wide OS `flock` under the real
deployment state directory. Acquire before fetch/build/host mutations and retain
through verification, rollback, and cleanup. A contending caller exits temporarily
unavailable with sanitized owner/phase metadata; it never recreates services.
Dry-run remains nonmutating and can run concurrently. Never unlink the lock inode.

Existing outer `flock rollout.lock bash deploy.sh` calls remain supported by
adopting an inherited descriptor only after matching the lock inode and acquiring
the same descriptor. Metadata or environment variables do not establish lock
ownership. Stale metadata does not block a free OS lock. Interrupted service
changes use the same protected rollback path before releasing ownership.

After tests, install one versioned clean deployment-tool worktree and a stable
`~/.local/bin/herdr-deploy` launcher. Backed-up routing shims in the known legacy
entry points delegate there, preserving their original bodies and other edits.
Missing canonical tools fail closed. Current checkout scripts also lock directly.

## Complete durable projection checkpoints

Raw `fabric_events` stays authoritative and is never deleted or type-filtered by
this feature. A versioned compressed JSON checkpoint contains core event-derived
records, delivery/usage/stage state, private identity/summary records, source-ledger
state, bounded inspector/host-metric state, and complete graph/collaboration replay
capsules needed by their owners. Locks, callbacks, clients, subscribers, and
process cursors are recreated. Mission-activity observations retain their existing
transient feed contract; a checkpoint cannot reconstruct payloads never persisted.

Projection format/code compatibility and checksums are verified before restore.
Invalid/incompatible checkpoints fall back to complete replay. Restore publishes
no events, commands, or new naming reservations. Retain the last valid checkpoint.
Structured-table overlays are re-read within each recovery snapshot and retain
the existing bridge precedence; do not bake stale structured rows into event-only
projection state.

The durable coverage frontier uses a server-assigned position allocated inside an
INSERT trigger **after** taking a shared transaction advisory lock. Existing rows
remain a legacy prefix. A checkpoint takes the exclusive session fence before
opening its repeatable-read snapshot, captures the frontier, then releases the
fence while reading that snapshot. This covers all database writers and prevents
an allocated-but-uncommitted lower position from being skipped. Insert/delete
changes identify dirty streams; unexpected updates invalidate unsafe fast paths.

Tail application must match complete replay, including lower/duplicate stream
sequences and late commits. Reconstruct affected streams from authoritative rows
where necessary instead of treating a partial payload as a complete record.
New durable positions define admission order for incremental ledger state;
legacy rows retain their prior deterministic ordering. Private-domain validation
and blocked ambiguous aliases remain intact.

Database-backed raw-history reads preserve full legacy streams and bounded page
reads, actor-scoped event identities, and private-stream denials. Recent activity
and SSE reconnect remain bounded and privacy-filtered. Explicit checkpoint CLI
preparation can run while the prior release is serving; subsequent checkpoints
use the same safe materializer and atomic publication protocol.

## Filtered and paged service catalog

Keep legacy `GET /api/v1/services` and `listServices()` intact. Add explicit
`view=catalog` requests with scope, display status, archive inclusion, host,
AND-token search, grouping, bounded limit, and opaque cursor. Catalog display
classification matches the current frontend, including stale listener evidence,
future timestamps, failed state, and verified origin scope.

Responses contain page rows plus exact whole-scope counts/host facets and exact
filtered totals/group summaries. Counts never derive from only the loaded page.
Use bounded short-lived snapshot cursors tied to normalized filters/order; stale
snapshots return a restartable 409. Freeze deterministic Unicode ordering and test
it explicitly; do not silently equate browser-locale ordering with Python sorting.
Read project mappings once per snapshot rather than once per service.

The UI appends pages, merges groups without duplicating rows, preserves open
groups/focus/scroll and current/history toggles, cancels superseded reads, keeps
last good data on failure, and expires unsafe endpoints locally. Detail refresh
fetches only the selected service. Existing full-list consumers continue working.

## Verification and release gates

- Concurrent deployment subprocesses: only one mutates, compatible outer lock,
  owner metadata, dry-run, stale-owner recovery, and interrupted rollback.
- Full replay equals checkpoint plus tail across every durable domain; no writes,
  broadcasts, private-feed leakage, or raw-row deletion during recovery.
- Real Postgres out-of-order commits, writer fencing, equal timestamps, duplicate
  stream sequences, backdated events, deletion/retention, failed publication,
  corrupt/incompatible checkpoints, and repeated restore.
- Catalog legacy compatibility, scope/status/search/archive parity, exact counts,
  cursor bounds/staleness, page/group continuity, cancellation, expiry, and focus.
- Production-sized read-only/candidate measurements for startup time/memory and
  catalog latency/payload. Validate live counts/history and normal UI after rollout.
- Deploy from a committed verified revision using canonical configuration and
  protected rollback. Preserve the original dirty checkout and unrelated work.
