-- First moment housekeeping saw the member missing from a FRESH directory listing; cleared when listed again.  The
-- removed-member grace is measured from here (the directory file only says who is listed, not since when).
ALTER TABLE member_usage ADD COLUMN removed_since timestamptz;
