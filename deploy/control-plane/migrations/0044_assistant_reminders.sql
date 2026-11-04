-- Reminders the assistant sets for a person ("remind me at 15:00"). Only the reminder text, the fire time and the state are
-- kept: never credentials. Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS assistant_reminders;   -- also drops its indexes

CREATE TABLE IF NOT EXISTS assistant_reminders (
  id         text PRIMARY KEY,
  tenant_id  text NOT NULL,
  user_id    text NOT NULL,
  text       text NOT NULL,
  fire_at    timestamptz NOT NULL,
  state      text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'fired', 'cancelled')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS assistant_reminders_due_idx ON assistant_reminders (fire_at) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS assistant_reminders_person_idx ON assistant_reminders (tenant_id, user_id, created_at DESC);
