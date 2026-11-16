-- Per-member session revocation and the lifetime of the identity recorded at sign-in.
--  sessions_valid_after: a member session issued at or before this instant is refused (set by sign-out).
--  identity_expires_at:  the recorded email and groups are only used until the session that vouched for them ends.
ALTER TABLE member_usage
  ADD COLUMN sessions_valid_after timestamptz,
  ADD COLUMN identity_expires_at  timestamptz;
