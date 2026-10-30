# Project intelligence: operator runbook

Everything is off until `control_plane.project_intelligence` in the central fleet YAML enables it. There are no
environment variables. Nothing here is deployed by the implementation; you roll it out.

## Configuration (all keys optional; defaults are off)
```yaml
control_plane:
  project_intelligence:
    enabled: false            # master switch; false = every route answers 404, no thread, no DB connection
    kill_switch: false        # hot: stops ingest, derivation, suggestions and execution immediately
    ingest: false             # accept events (bridge, webhook, Gmail engine)
    derive: false             # create inferred (provisional) relationships
    suggest: false            # allow humans to confirm/reject inferences via the API
    execute: false            # reserved for proposals; no executor exists in this release
    auto_merge: false         # reserved; the repair gate below is the only merge control
    default_org_id: ""        # org for the open loopback operator and for bridge/webhook events
    viewers:                  # API key principal -> scope ("key:<first 12 hex of sha256(key)>")
      "key:abc123abc123": {org_id: lab, projects: ["p-binbase"], clearance: internal}
    assistant_viewers:        # assistant "<tenant>/<user>" -> scope; absent means the tool returns nothing
      "t1/u1": {org_id: lab, projects: "*", clearance: internal}
    forge_host: github.com    # lets bare owner/name CI events resolve to a forge identity
    webhook_secret_file: /etc/herdr/github-webhook-secret   # enables POST /api/v1/intelligence/webhooks/github
    webhook_projects: {"owner/repo": ["p-binbase"]}         # unmapped repos are acknowledged, never stored
    egress:                   # model-routing classes (X-Herdr-Data-Class tagged requests only)
      upstream_kind: local    # local | approved_cloud | external ; unset = external (public data only)
      custom_kinds: {my-model: approved_cloud}
      approved: [internal]    # classes a cloud route may carry; mail/scientific/restricted are always local-only
    repair:                   # gate for herdr-ci-fix/ PRs; absent/enabled:false = merges behave exactly as before
      enabled: false
      kill_switch: false
      auto_merge_enabled: false
      allowed_repositories: [owner/repo]
      allowed_branches: [main]
      auto_merge_risks: [low]
      max_patch_lines: 200
      require_human_approval: true
      model_families: {claude: anthropic, gpt: openai}   # model-name prefix -> family; unlisted = unknown = policy waits
```

## Safe enabling order
1. Apply migration `0020_project_intelligence.sql` (its `-- DOWN:` drops only the `pi_*` tables).
2. `enabled: true` only. Check `GET /api/v1/intelligence/status` (operator): `metrics.bridge`, `queue_depth`, `incidents`.
3. `ingest: true`. Run `EventBridge.backfill(repo)` in dry-run first; `unmapped` is what no project could be resolved for.
4. `derive: true`, then `suggest: true` once a person is ready to review inferred links.
5. `repair.enabled: true` with `auto_merge_enabled: false` (observe `repair policy:` statuses on herdr-ci-fix PRs).
   Auto-merge also needs, per PR: a person's approval of the current head, every commit signature verified by GitHub, an author session known to the
   gate, and reviewer and author models from different listed families. Anything missing makes the gate wait for a human.
6. Missed webhook deliveries: `POST /api/v1/intelligence/webhooks/reconcile` (operator, org-wide) records open PR heads no webhook saw.

## Proposals
With `suggest: true` an operator can create a proposal (`POST /api/v1/intelligence/proposals`, optional `Idempotency-Key`); a *different* operator approves it with the
exact `payload_sha256` and `resource_version` it was shown. Proposals expire after 30 minutes and are not persisted. Approving records permission only.

## Stop / rollback
- Fastest: `kill_switch: true` (config is re-read within seconds). `repair.kill_switch: true` rejects repair merges.
- If repair was enabled and the central config becomes unreadable, the gate rejects (it never falls back to "allow").
  After a control-plane restart with unreadable config it behaves as disabled (merges as before): fix the config.
- Pause one source: `POST /api/v1/intelligence/sources/<source>/pause` (org-wide viewers only).
- Remove: set `enabled: false`; run the migration's `-- DOWN:` only when the data is no longer wanted (confirm first).

## Drills (each has an automated test; run them before relying on a control)
| Drill | Test |
|---|---|
| Disabled means 404 and no side effects | `test_project_intelligence_api.py::test_disabled_by_default_is_404_everywhere` |
| Webhook signature, replay, unmapped repo, kill switch | `test_intel_webhook_route.py` |
| Repair gate inert / fail-closed / unreadable config | `test_repair_gate.py` |
| Gmail revoke tombstones and pauses; backfill idempotent | `test_intel_gmail_sync.py` |
| Egress: mail never reaches a cloud route | `test_llm_proxy.py::test_tagged_content_is_gated_by_operator_egress_routes` |
| Incident states never move backwards | `test_intel_incidents.py` |
| Whole enabling order, restart, kill switch, DOWN | `test_intel_rollout_rehearsal.py` |
| Approval needs another person and the exact payload | `test_project_intelligence_api.py::test_proposals_*` |
| Attachments: only accepted bytes reach the artifact store | `test_intel_connectors.py` |

## Egress gate scope
`X-Herdr-Data-Class` is honoured only when the caller sends it. Untagged requests are not gated, so the gate protects cooperating callers (the assistant and intelligence tools), not a hostile one. A custom model is allowed only if the content may go to both its own route and the shared upstream.

## Who may approve (single-operator tailnet)
`control_plane.project_intelligence.approvers: [<principal id>, ...]` names the only principals that can approve or reject a proposal. Absent or empty means nobody can; an open listener never can (no identity). Give the approver key ONLY to your iPhone and Mac, keep it out of every agent's environment, and give agents a different key that can request but not approve. The tailnet keeps outsiders away; this list keeps agents on the tailnet from approving their own requests. Requests are already refused when the approver is the requester.

## GitHub without a webhook (tailnet-only)
GitHub cannot reach a tailnet, so use outbound polling: set `project_intelligence.reconcile_interval_seconds` (integer, at least 60; absent or anything else = off), plus `webhook_projects` (repo -> project ids) and `default_org_id`. Every interval the dashboard records open PR heads no event has recorded. It runs only while `enabled` and `ingest` are on and the kill switch is off. Latency is up to one interval; CI failures arrive through fabric events regardless. `POST /api/v1/intelligence/webhooks/reconcile` still runs one pass by hand.

## Single-operator mode (one person, private tailnet)
`control_plane.single_operator: true` (YAML, default false) lets the open listener's caller act as the operator: the
assistant's `preauthorized` grants are accepted, a proposal can be approved or rejected without an `approvers` list, and the
requester may approve their own proposal (the exact `payload_sha256` and `resource_version` are still required). It also
turns on `personal_assistant.actions.allow_self_approval`. `send_message` and `run_shell` still always need an approval.
The server refuses to start with `single_operator` and `api_keys` together; with keys the normal identity rules apply.

Residual risk: on an open listener every process on the tailnet is "the operator", including coding agents, so any of them
can grant a tool or click approve. Use it only while that is acceptable; the audit log still records each decision.

## Pull-request polling without the review gate
With `reconcile_interval_seconds` set, the poller uses the review gate's GitHub client when the gate is on, otherwise the
shared credential (`github.app`, else `github.token`) with a repo-scoped `pull_requests: read` token. The GitHub App must have
"Pull requests: Read-only" and the installation must accept it. `GET /api/v1/intelligence/status` (operator) shows
`poller`: the latest pass counters and `last_error` (`permission`, `unknown_owner`, `not_configured`, `not_found`, ...).

