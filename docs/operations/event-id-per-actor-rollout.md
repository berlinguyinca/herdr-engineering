# Rollout: event_id unique per principal (migration 0005)

`0005_event_id_per_actor.sql` replaces the global primary key on `fabric_events.event_id` with a unique
index on `(COALESCE(actor->>'id',''), event_id)`, so one API key cannot squat another key's
(guessable) event ids.

## Order

1. Deploy the control plane that contains this change. Its inserts use a bare `ON CONFLICT DO NOTHING`,
   so it runs against both the old schema (global key, stricter) and the new one.
2. Apply `0005_event_id_per_actor.sql` (idempotent, safe to re-run).

Applying the migration before step 1 breaks ingest of the PREVIOUS release: it issues
`ON CONFLICT (event_id)`, which needs a unique index on `event_id` alone, so every durable insert fails
with "there is no unique or exclusion constraint matching the ON CONFLICT specification".

Until step 2 the old global key still holds. A different principal reusing an event_id then receives
`409` with `event_id '...' is already used by another principal (apply migration 0005...)` instead of the
misleading sequence-collision text.

## Rollback

Rolling the code back below this release after step 2 requires restoring the global key first:

```sql
-- must return no rows, otherwise two principals share an event_id and need resolving first
SELECT event_id FROM fabric_events GROUP BY event_id HAVING count(*) > 1;
ALTER TABLE fabric_events ADD CONSTRAINT fabric_events_pkey PRIMARY KEY (event_id);
DROP INDEX IF EXISTS uq_fabric_events_actor_event_id;
```

(The in-repo `fabric_events_pkey` name is the default Postgres gives the original primary key.)
