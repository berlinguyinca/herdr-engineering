-- Agent scope: an agent token may mutate only artifacts (and shares) created through an agent token.
--  Existing rows are 'web', except pending artifacts, which could only have been pushed by an agent.
ALTER TABLE artifacts ADD COLUMN created_via text NOT NULL DEFAULT 'web' CHECK (created_via IN ('web', 'agent'));
UPDATE artifacts SET created_via = 'agent' WHERE intake = 'pending';
ALTER TABLE shares ADD COLUMN created_via text NOT NULL DEFAULT 'web' CHECK (created_via IN ('web', 'agent'));
-- Idempotent MCP writes (publish, share_create) keep their summary so a retry returns the same answer.
ALTER TABLE artifact_member_creates ADD COLUMN result jsonb;
UPDATE agent_tokens SET label = left(label, 64);
ALTER TABLE agent_tokens ADD CONSTRAINT agent_tokens_label_len CHECK (char_length(label) <= 64);
