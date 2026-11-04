# Migration, deployment and rollback

## Inventory before changes
Record every installed Pi memory profile, OpenViking endpoint/config, Blackhole durable store, Hindsight banks, version, backup, reference counts and active instances. Check app personal memory isolation, installed package version, token scope and actual server health. Take backup/export using the canonical engine's supported mechanism (do not invent raw DB schema). Preserve original ID, provenance and timestamp when supported.

## Data migration
Implement read-only dry-run comparator (records, duplicates, conflicts, deletions); choose explicit mapping rules after confirming provider APIs. Never copy personal-assistant data to global engineering. Migrate in batches with idempotency IDs and reconciliation; dual-read shadow mode acceptable, dual-writing discouraged unless proven deterministic. Confirm deletion/tombstones propagate. No destructive legacy removal until rollback window and data parity acceptance.

## Rollout
1. Analyze current fleet lock and changes in `herdr-fleet` and `herdr-engineering`.
2. `memory plan` and `doctor` on isolated HOME/test harnesses; show diff.
3. Select one idle canary host (not user-blacklisted laptop); preserve running sessions and credentials.
4. Deploy a pinned adapter revision; run live store/recall, ACL denial, cache invalidation, handoff and UI smoke tests.
5. Only after explicit operator approval and existence of session-aware rollout/rollback capability, stagger remaining hosts.

## Rollback
Disable managed adapter via feature flag; restore managed configuration backups only (never overwrite pre-existing user changes); reinstall previous pinned adapter; keep Hindsight data intact and avoid destructive DB migration. If client rollback breaks new schema use backwards-compatible server/version protocol negotiated in P00. Audit rollback and show fleet drift until fixed.

## Secret/security checks
No token in command-line arguments when avoidable, output, git, logs or ZIP. Credential file mode 0600, parent 0700; prevent same-user agent-shell spoofing being treated as trusted harness attestation. Restrict Hindsight to private networks and authorized worker endpoints. No production changes without authorization.
