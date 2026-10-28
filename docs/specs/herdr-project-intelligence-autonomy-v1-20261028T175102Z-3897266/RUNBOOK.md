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
```

## Safe enabling order
1. Apply migration `0020_project_intelligence.sql` (its `-- DOWN:` drops only the `pi_*` tables).
2. `enabled: true` only. Check `GET /api/v1/intelligence/status` (operator): `metrics.bridge`, `queue_depth`, `incidents`.
3. `ingest: true`. Run `EventBridge.backfill(repo)` in dry-run first; `unmapped` is what no project could be resolved for.
4. `derive: true`, then `suggest: true` once a person is ready to review inferred links.
5. `repair.enabled: true` with `auto_merge_enabled: false` (observe `repair policy:` statuses on herdr-ci-fix PRs).
   A bare deployment cannot auto-merge: reviewer independence, signed provenance and human approval are supplied by
   `repair_gate.make_repair_gate(facts_fn=...)`, and the default supplies none.

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
| Attachments: only accepted bytes reach the artifact store | `test_intel_connectors.py` |
