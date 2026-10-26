# Phase 1 — safety, host identity, eligibility
Introduce stable `host_uid` (generated once on a machine; persisted as 0600 host identity) and alias resolution to avoid opt-out bypass via SSH target or renamed machine; detect clones/conflicting identities and fail closed. Host identity proof must be authenticated over existing trusted transport; do not trust client-provided hostname alone. Keep aliases for display.

## Modes and defaults
`automatic`: scheduler may select under capacity; `manual_only`: only authorized explicit selected host, never inherited; `draining`: no new launches and existing ones finish; `disabled`: deny ordinary new launches including manual. A separate documented break-glass administrator route can exist but must be audited, not silently inferred. `quarantined` operational state always blocks launches. Existing `auto_agents=false` maps to `manual_only` without relaxation; preserve backward compatibility until migrated. If unknown policy, stale policy store, duplicate identity, unreachable, untrusted, or policy read fails: no new auto placement. Never interpret absent allowlist as universal permission where the fleet lacks verified ownership.

## Required rules
* Mac laptop stable UID must be `manual_only` by default, including AC power, background wake, reconnection, renamed aliases, command-line launches and child spawns. Detect portable Macs with advisory platform capability; owner-specific explicit persisted override only for broader automatic eligibility (require clear warning); initial requested Mac policy remains manual only.
* Manual host selection is explicit by human/authorized scheduler API with provenance, and still validates capabilities, security and safety. A parent on Mac does not pass through inherited host as an explicit override.
* Include per-host allow/deny tags, per-user/workspace/project/agent-family allow and deny, temporary maintenance exclusions with expiry, hard group allowlist. Deny always wins.
* Never place on external collaborator host merely because they appear in room, agent graph or federation.
* Policy version + ETag/concurrency check; mutations require operator permissions and durable audit; CLI read/write and UI share one backend.
* Predict host online/offline based on trusted heartbeat; lease expiry blocks placement.

## Tests / acceptance
Rename/re-register Mac multiple aliases; spawn locally/manually then create three children; verify none auto-scheduled to Mac. Test disabled/draining, stale/unreachable hosts, conflicting identity and race with policy flip between rank and bind. No bypass through any launch route.
