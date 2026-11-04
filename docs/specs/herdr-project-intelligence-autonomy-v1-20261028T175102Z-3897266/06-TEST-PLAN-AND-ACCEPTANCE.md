# 06 — Automated test and acceptance matrix

## Test fixtures (synthetic, versioned)
- Gmail thread with MIME alternatives, replies/out-of-order timestamps, multiple plot attachments, revised spreadsheet, quoted old text and explicit issue link.
- Similar but unrelated email subject in different project to measure false linking; anonymized participants with same display name.
- GitHub webhook stream: duplicate, delayed, out-of-order, tampered signature, changed PR SHA, merge queue retries, missing event recovered by reconciliation.
- CI failures: flaky runner, dependency/network outage, regression with reproducible test, sensitive one-line auth fix, missing mandatory checks, canceled workflow.
- Scientific data: peak-picking plot contradicts earlier hypothesis and tests pass yet domain validation fails.
- Agent runs: stalled loop, interrupted process, retried action, reviewed code, artifact creation and permission denied.
- Auth fixture with separate personal/work/guest project scopes and revoked access.

## Required automated suites
1. Unit: stable event IDs, envelope validation, actor resolution, authority precedence, confidence, semantic dedupe, policy evaluator.
2. Integration: Gmail cursor/backfill/replay/revoke, GitHub webhook signatures/reconcile, artifact registry versions, transactional inbox/outbox, OpenViking index deletion.
3. Contract: tool gateway permissions, typed dashboard navigation, source link validity, identity scoping, authorized merge actions.
4. E2E: email -> attachments -> artifact collection -> investigation -> graph -> timeline -> GitHub issue -> PR -> tests -> decision -> notification.
5. E2E: signed CI failure -> classified regression -> isolated reproduction -> Pi implementer -> PR -> independent reviewer -> required checks -> deterministic merge -> post-merge verify -> email.
6. Security: prompt injection in emails, scripts in SVG/HTML, zip bombs, tainted CI commands, forged approval, malicious webhook, cross-tenant search, privileged tool attempts.
7. Reliability: duplicate and out-of-order replay, disconnections, exhausted queues, worker crash between side effect and acknowledgement, missing credential, backfill resume, event version upgrade.
8. UI: desktop and narrow iPhone screenshots at dark/light modes, accessible labels, deep links, proof of no confidential text in restricted views, persisted filters.

## Initial measurable quality gates (targets, adapt after fixture audit)
- Event processing at-least-once with no duplicate *effective* side effects in replay tests; all mutation endpoints require idempotency keys.
- 100% of generated material findings have ≥1 valid evidence reference in mandatory fixtures; unsupported findings are marked unverified rather than rendered as facts.
- 0 unauthorized merges in negative policy fixtures; 0 branch protection bypasses; required check and head SHA revalidation on every merge.
- 0 cross-scope content leaks in tenant-isolation E2E and API tests.
- 100% of synthetic source objects are retrievable through authorized provenance links (unless revoked/tombstoned).
- No generated scientific claim treated as validated solely by software CI.
- Backfill and missed event reconciliation produce equal normalized entities/edges for equivalent source history in deterministic fixtures.
- UI screens for all defined primary flows pass responsive and keyboard accessibility tests.

## Scorecard
Record per version: extraction precision/recall, edge precision/recall, owner attribution accuracy, event recovery, CI root cause class accuracy, autonomous repair fix success, reviewer catches, merge false positive, incident resolution time, notification usefulness, GPU/token cost. Compare against a naive no-intelligence baseline; store model and prompt versions. Require manual sample audits before production auto-merge enablement.

## Definition of done
Code, migration/rollback, docs, tests, instrumentation, dashboards, security review, verified role scoping, feature flag, no duplicated services, and demonstration from browser and iPhone. CI auto-merge remains disabled until formal project policy authorization and negative tests pass.
