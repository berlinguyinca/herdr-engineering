# Event recovery checkpoints

The current PostgreSQL journal remains authoritative, including retention and
collaboration quarantine. Checkpoints retain original events and exact actor/event
identity coverage; they do not delete, rewrite or replace journal history.

Apply migrations before building. On a large database, build the arrival and
identity indexes concurrently using the instructions in migrations0014/0015 and
verify both `indisvalid` and `indisready` before running the transactional migrator.
Never build an index or checkpoint automatically during HTTP startup.

Migration0015 also adds an internal nullable `recovery_instance` column and a
trigger assigning a new UUID on every insert/update. Existing rows keep NULL;
there is no historical payload backfill or table rewrite. After applying0015,
build `idx_fabric_events_recovery_instance` CONCURRENTLY with the definition in
0016, verify it, then apply0016's receipt. The covering index allows checking
insertion instances without reading every historical payload. No client can
preserve an old insertion instance when reusing an actor/event identity.

Run the initial builder while the existing server continues serving requests:

```bash
python3 -m herdr_engineering.control_plane.recovery_checkpoint --local-port 15433
```

The command reads the private central fleet configuration, overrides only the
PostgreSQL host/port when `--local-port` is supplied, and prints counts, generation
identity, format and elapsed time. It never prints credentials or payloads. Use
`--fleet-config PATH` for another private file and `--timeout SECONDS` to set the
offline statement timeout (default1800s). A missing checkpoint schema fails before
publication. One builder is allowed at a time; ordinary ingestion is not locked.

Coverage includes actor/event identity and insertion instance. Reusing a deleted
identity or updating a historical row is detected as replacement, including rows
that predate the migration. Coverage and selected rows share a repeatable-read snapshot. The active pointer is
published atomically with both sets; errors leave the previous generation active.
A transaction committed after that snapshot is uncovered even if its timestamp,
sequence or transaction start predates the builder. Actor domains remain separate.
Existing readers continue using their old generation consistently.

Startup uses a compatible generation plus the exact uncovered tail. Retained rows
removed from the live journal are excluded. Deleted compacted snapshots and late
arrivals inside a compacted stream require replaying that stream's current full
history, so omitted interior observations become available again. Unaffected
streams keep using their checkpoint boundaries. All private and v2 ledger events
are retained. Original journal IDs, timestamps and sequences are preserved.

Absent/incompatible generations fall back to faithful full recovery. This is not
a universal time bound: large tails, private histories, and affected streams may
still be expensive. Measure both event-read and complete repository hydration at
the actual deployment scale before cutover. Rebuild offline after bulk repair,
retention, or a large tail, and when changing the compaction format. Do not lower
readiness requirements or omit streams to make a deployment appear healthy.

Old generations are retained for readers and rollback; no automatic pruning is
performed. Keep the underlying journal for authoritative recovery and audit.

Projection compaction does not truncate history APIs. Host, session and service
history reads use the raw PostgreSQL journal: indexed keyset pages for paginated
requests and the complete stream for legacy requests. Private streams never use
that reader. Database failures remain explicit failures, not incomplete cache
fallbacks. Cursor fingerprints use a process-private HMAC so masked low-entropy
values cannot be guessed from public cursor hashes. Cursors can become stale
after a server restart; reload history when the API returns409.
