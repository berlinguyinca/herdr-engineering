-- One 'expired' event per (share, expiry instant).  The event's `at` is the expires_at it records, so an owner who
-- extends a share and lets it expire again gets a new event, while a repeated sweep cannot add a second.
-- Duplicates written before this index existed are removed first (the earliest event of each pair is kept).
DELETE FROM share_events e USING share_events k
 WHERE e.kind = 'expired' AND k.kind = 'expired' AND e.share_id = k.share_id AND e.at = k.at AND e.event_id > k.event_id;
CREATE UNIQUE INDEX ux_share_events_expired ON share_events (share_id, at) WHERE kind = 'expired';
