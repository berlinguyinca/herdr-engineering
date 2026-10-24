"""Event-log entity types owned by the review gate (import-free on purpose).

Generic ingestion (``control_plane.ingest``) refuses any entity type that starts
with one of these prefixes: the gate's state machine, review queue and author
pins are written only by the gate itself, never by an ingest/agent API key.
Every gate stream (``pr_gate``, ``pr_gate_author``, ``review_job``) must start
with one of them.
"""

RESERVED_ENTITY_PREFIXES: tuple[str, ...] = ("pr_gate", "review_job")
