-- The verified identity of a member as last seen at sign-in (set from verified Cognito tokens only, never from a
-- request body).  The library feed needs the email and groups to show lab shares addressed to the member.
ALTER TABLE member_usage
  ADD COLUMN email  text,
  ADD COLUMN groups text[] NOT NULL DEFAULT '{}';
