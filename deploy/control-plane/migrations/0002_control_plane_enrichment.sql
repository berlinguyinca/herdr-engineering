-- Phase-2 control-plane enrichment (Spec §53 extension): persist live session
-- and mission detail so the UI can show what each session is doing, where it
-- runs, its git branch, its pid, and how to attach to it.

-- Missions: record the workspace project directory.
ALTER TABLE missions ADD COLUMN IF NOT EXISTS cwd text;

-- Sessions: record live operational detail captured from the host's herdr CLI.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS cwd text;       -- pane working directory
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS branch text;    -- current git branch in cwd
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pid text;       -- foreground agent pid
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS cmdline text;   -- running agent command line
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary text;   -- terminal title (what it's doing)
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS attach text;    -- command to attach to the session
