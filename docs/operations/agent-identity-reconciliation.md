# Repair conflicting agent-name history

Restore naming reservations before allowing allocation. Earlier startup filters
omitted private naming events and repeatedly allocated version1 identities,
leaving conflicting names/handles and divergent versions in the durable journal.
Ordinary recovery remains strict and refuses unexplained conflicts.

Generate a private, read-only proposal:

```bash
python3 -m herdr_engineering.control_plane.agent_identity_repair_cli --local-port 15433 plan --out /private/identity-plan.json
```

The plan file is created exclusively with mode0600. It contains the complete
before/after mapping, exact covered event IDs/content digests, policy version,
corrections and retired aliases. Standard output contains counts and a repair ID,
never names, credentials or configuration. Inspect the file privately.

The deterministic policy preserves every identity ID and uncontested historical
reservation. An alias claimed by multiple identities is permanently blocked for
all claimants; no identity silently inherits it. Display names/derived handles are
disambiguated only when needed. Changed records use a version above the historical
maximum, including histories where a later process reset its version counter.
Every original journal event remains intact.

Before applying, finish validation of the proposed release and stage its immutable
image. Stop old naming writers that do not implement the durable transaction
fence; the worker does not allocate names. Generate a fresh plan after that fence
so a newly recorded identity cannot make the proposal stale. Apply explicitly:

```bash
python3 -m herdr_engineering.control_plane.agent_identity_repair_cli --local-port 15433 apply --plan /private/identity-plan.json
```

The operation takes the same advisory transaction lock as naming writers and a
short journal write fence. It rechecks the exact input and appends compensating
records plus one private reconciliation marker in one transaction. Stale/tampered
plans and partial/conflicting retry state are rejected. Repeating an already
committed plan validates it and writes nothing.

Recovery verifies the marker, its covered history and every correction before
using the reconciled baseline. Uncovered events still undergo strict validation,
including backdated transactions. Complete naming history and blocked aliases
survive export/import and full/checkpoint recovery. They stay outside activity,
global SSE and generic ingestion. No new HTTP repair endpoint is provided.

Durable allocation/rename now serializes reservation checks and publication in a
database transaction. Independent registries cannot reserve the same alias or
overwrite a newer version. A duplicate allocation returns the actual durable
event to the caller. In-memory publication happens only after acknowledgement.

Deploy the naming-aware release immediately after applying. Do not reopen an old
writer that omits restored reservations: it can generate another conflicting
history. Keep the private proposal and release/repair receipts for audit. Measure
actual complete repository startup; a checkpoint does not make meaningful
history or retained inspector replay free.
