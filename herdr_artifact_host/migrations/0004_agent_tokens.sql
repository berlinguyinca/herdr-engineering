-- Agent tokens (MCP bearer secrets): the table exists since 0001 with only (digest, member_id, created_at, revoked_at).
-- Add the listing identity (token_id, label, last_used_at).  The secret itself is never stored, only its digest.
ALTER TABLE agent_tokens
  ADD COLUMN token_id     text NOT NULL DEFAULT 'atk_' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 16),
  ADD COLUMN label        text NOT NULL DEFAULT '',
  ADD COLUMN last_used_at timestamptz;
CREATE UNIQUE INDEX ux_agent_tokens_token_id ON agent_tokens (token_id);
