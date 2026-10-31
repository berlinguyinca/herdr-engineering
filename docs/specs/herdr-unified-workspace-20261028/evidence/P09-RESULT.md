# P09 result: PARTIAL (docs only)

Spec: `phases/P09.md`. Gate: all phases signed off, compatibility and migration tests green, operations can revert frontends safely.
**The gate is NOT met.** P07 and P08 are not started and P03-P06 are partial, so no sign-off is possible. This file records the
documentation slice only. Nothing here was run: no test, chaos, load, accessibility or security tool was executed.

## Delivered (documents)
| Spec requirement | Where | Status |
|---|---|---|
| Threat model | `security/THREAT_MODEL.md`, section "Implementation threat model" (assets, boundaries, surfaces, mitigations, residual risks with severity). Covers work graph routes, artifact registry/ACL, room-derived read access, share links (lab vs public), crash-takeover leases, collab public listener, control-plane deploy | Written from code reading only |
| Documented rollback, feature flags | same file, section 5: per flag turn-on, watch list, turn-off and revert order for `unified_workspace.enabled`, `unified_workspace.room_artifact_access`, `control_plane.collab.task_lease_seconds`, share links | Written, not rehearsed |
| Accessibility pass | same file, section 6: a checklist for screens that do not exist | Checklist only, not an audit |

## Not done (all of the rest of P09)
- Two-host plus browser E2E scenario, crash replay, restore from backup, revisit old project.
- Chaos tests (partial publish, dropped socket, reordered event, expired token, concurrent update, ACL revoke during stream,
  compromised agent prompt, host migration, stale approval, ambiguous email send). Some of these map to existing P03/P05 unit
  tests (see their result files) but were not re-run for this phase.
- Performance and load tests on large graphs and high event volume (P06 has one 4,001-node grouping test; not a P09 load test).
- Trace ids end to end and metrics; token-redaction review of logs (only the share server's no-log design was read).
- XSS, SSRF and sandbox-preview security tests (the threat model lists the risks; no test was written or run).
- Per-user feature flag rollout (not built; every flag is global, YAML, start-time).
- Repair command and runbook for room-derived grants, backup restore check, measured UX numbers.

## Risks found that need a code fix (listed, not fixed)
1. Task lease fence is opt-in (`collab.py` `update_task` checks `attempt` only when sent) and the lease has no heartbeat. [R-TL1, R-TL2]
2. Room-derived access overwrites other-source grants and has no repair command to remove `room:*` grants when the flag is turned off. [R-RA2, section 5]
3. Room revoke is asynchronous with no retry timer after a failed sync. [R-RA1]
4. No unlink for an artifact-room link; linking needs only `edit`. [R-ART3, R-RA3]
5. Member graph door and room-artifact access are wired only when collab is not `--collab-only`; the documented compose topology runs collab only in `--collab-only` mode, so these cannot run there. A topology decision is needed, not a patch. [R-WG1, R-RA4]
6. Public share: single-principal approval, no notification; sandbox allows inline script and self-navigation. [R-SH1, R-SH2]
7. Legacy no-owner artifacts are readable by all viewers; no ACL means allow-all. [R-ART1]
8. Shared layout write has no room-role check. [R-WG2]
9. Lab share has optional expiry and room snapshot outlives membership; room id probing. [R-SH4, R-SH5]

## Rollback
Delete the two documents and the STATUS and CHANGELOG lines; no code, schema or config changed.
