# Lineage / assistant integration: release checklist

Tick each line with evidence (command output or link). Nothing here is automatic; merging and deploying are manual.

## Before merge
- [ ] PR chain merged bottom-up: #365 (base), then the artifact-scope/CI/metrics PR. CI green on the exact head (Woodpecker).
- [ ] `python3 -m pytest tests/control_plane/test_artifact_scope.py tests/control_plane/test_artifact_lineage.py tests/control_plane/test_artifact_render_lineage.py tests/control_plane/test_email_attachment_artifact.py tests/control_plane/test_ci_repair_rereview_gate.py tests/control_plane/test_lineage_metrics.py tests/control_plane/test_review_quorum.py -q`
- [ ] Full `tests/control_plane` sweep (needs a local Postgres for the e2e files).
- [ ] `ruff check` clean on touched files (`graph.py` and `api.py` carry pre-existing findings only).
- [ ] The review-quorum vectors still byte-identical to AutoSpec `origin/main` (`sha256sum` both).
- [ ] Independent review of the diff by someone other than the author.

## Before enabling for users
- [ ] `personal_assistant.enabled` stays false until the isolation tests pass on the target build.
- [ ] `artifact_acl` is configured on the control plane (otherwise artifact nodes are `private`, committer only).
- [ ] Artifact smoke: commit a version with key A; key B (no grant) gets 404 on `/api/v1/graph/nodes/artifact:<id>`; grant B `read` -> 200;
      revoke -> 404; restart the control plane and repeat.
- [ ] `insights.evidence_writers` set for rooms that should show accepted changes / gate failures (else those read `unknown`).
- [ ] Operator knows the rollback order (runbook) and that pre-change artifact nodes stay `private`.

## After deploy (manual, by the operator)
- [ ] Room analytics returns `delivery`; tokens/cost read `unknown` for emitters that report none.
- [ ] No new 5xx in the control-plane log for graph reads of `artifact:` nodes.

## Not covered by this release (do not claim)
Email-attachment ingestion has no route or scheduler; retraction has no HTTP route; the render worker process cannot reach the
ingest channel; the assistant tool does not read metrics; no connected-UI screenshots exist for any of this.
