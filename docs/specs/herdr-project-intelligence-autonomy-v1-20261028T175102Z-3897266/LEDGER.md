# IMPLEMENTATION LEDGER

Branch `feat/project-intelligence-autonomy-20261028T175102Z-3897266` (worktree `~/IdeaProjects/herdr-pia-20261028T175102Z-3897266`), base `origin/main` @ `ad0aa79`.
Nothing is pushed, merged or deployed. All flags default off. No live database, mailbox, GitHub App or API key was touched.
Acceptance criteria are **NOT yet met** — see "Remaining work".

## What already existed (reused, not duplicated)
`fabric_events` + envelope v2, `repo.record_event`, `project_links.py`, `ArtifactStore`/RustFS, `ci_fix` + `ci-fix-runner.py`, `review_gate` (`decide_merge`, App client, `main_verify`), `host_policy`/scheduler, `assistant.py` tool gateway, `attention_inbox`, `entity-links.js`, the dashboard shell/design system. See AUDIT.md.

## Phase status
| Phase | State | Changed files | Tests (measured) | Gaps |
|---|---|---|---|---|
| P0 audit | **done** | AUDIT.md, INTEGRATION-MAP.md, DECISIONS.md, this ledger, spec bundle | n/a | deployed capabilities unverified (B5) |
| P1 event foundation | **mostly done** | `control_plane/project_intelligence.py`, `project_intelligence_api.py`, migration `0020_project_intelligence.sql`, small glue in `api.py` | 16 + 6 unit/route tests; migration up / re-run / constraint / down on a private Postgres 18: pass | `DurableIntelligenceStore` (write-through, hydrates on start, rolls memory back on a failed write) + `EventBridge` (live `ci_fix` events, bounded queue + worker, opt-in repeatable `backfill`) now exist and run against a private Postgres (11 integration tests). Still open: accepted events are **not re-emitted into `fabric_events`** (the bridge *reads* it; intelligence rows are the projection); no `Idempotency-Key` header on writes; hydration loads every row (needs paging at scale); artifact events are intentionally unmapped (no server-verified project linkage yet). |
| P2 connectors | **partial (pure cores only)** | `intel_webhook.py`, `intel_attachments.py`, `intel_gmail.py` | 27 tests: signature valid/tampered/missing/unconfigured, replay guard, changed PR head, cancelled workflow, zip bomb, traversal, macros, SVG script, type mismatch, injection flagged, same-name actors, explicit links | **No Gmail fetch/OAuth/cursor/backfill (B1)**; **no webhook HTTP receiver or reconciliation (B2)**; attachments are triaged but **not registered into the artifact registry**; no thumbnails; no scientific parser; no adapter for `ArtifactRegistered`/`ci_fix` events; no OpenViking/herdr-memory sink or deletion propagation (D5) |
| P3 intelligence | **partial** | same store | relationships (explicit/inferred/review/versioning), findings w/o evidence marked unverified, tombstones, as-of | no extraction pipeline, no commitments/decisions records, no entity merge/alias, no precision/recall evaluation |
| P4 dashboard/mobile | **partial** | `packages/project-intelligence-view.js`, `app.js` (1 NAV + 1 ROUTES + view fn), `entity-links.js`, `api.py` capability, `tests/playwright/project-intelligence.test.mjs` | 6 Playwright tests (nav gated by capability, XSS-escaped titles, evidence pane, URL-persisted filters, keyboard, empty/error states, phone no-overflow, dark/light) + 707 web-client tests pass; screenshots in `evidence/` | no graph view, no investigation/artifact-provenance pages, no assistant side panel/approval cards in UI, no iPhone verification (B4), no axe accessibility run |
| P5 autonomy | **partial (policy core + optional gate hook)** | `repair_policy.py`, `intel_assistant.py`, `intel_egress.py` | 45 policy tests (20 negative gates, risk by path not size, deny class, kill switch, budgets, combine never widens, no model-authority input, incident machine, breaker) + 10 assistant/e2e | `review_gate/service.py` has an optional, default-None `repair_gate` hook applied only to `herdr-ci-fix/` branches (fail-closed; 5 tests incl. 'non-repair PRs unchanged'), but **nothing constructs the gate yet**: no production `evidence_fn` (changed-file list incl. rename old paths, signed provenance, human-approval count, reviewer model family); incident machine is not persisted nor fed by `ci_fix` events; no classifier for flaky/infra/regression; no auto-trigger; no email sender (B3); assistant tools are **not registered in `TOOL_CATALOG`**; `llm_proxy` does **not** call `may_route` yet |
| P6 backfill/prod | not started | — | — | opt-in repeatable backfill, observability metrics, runbooks, drills |


## Independent review (fresh agent, diff `origin/main...HEAD`, not shown my conclusions)
Verdict: no way found to widen what merges today, no SQL injection, no XSS, HMAC compare correct. Findings and disposition:

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | Med | `classify_risk` returned "low" for `docs/CODEOWNERS`, `src/README_loader.py`, `pkg/docs/hooks.py`, nested `SKILL.md` (glob `*` crossed `/`, README/CHANGELOG prefix match) | **Fixed**: real glob matcher (`*` never crosses `/`, `**/` any depth), docs anchored at root, agent-instruction/skills/prompt files are high. 21 regression cases. |
| 1b | — | (found while fixing) `lstrip("./")` stripped the dot of `.woodpecker.yml`/`.env` | **Fixed**, tested |
| 2 | Med | `.woodpecker.yml`, Dockerfile, Makefile, `*.sh`, `.npmrc`, `id_rsa`, `*.p12`, case (`.PEM`) only "moderate"/"low" | **Fixed** (paths added, lower-cased). Renames/deletions: documented on `Change.paths` that callers MUST pass old paths — **not enforceable here**, belongs to the production `evidence_fn`. |
| 3 | Med | Incident machine stuck after BLOCKED→AWAITING_APPROVAL; rollback after COMPLETED impossible | **Fixed**, 4 tests |
| 4 | Med | Unknown reviewer/author model family counted as independent; negative sizes; unvalidated `auto_merge_risks` | **Fixed** |
| 5 | Med | Only `append_event` rolled back on DB failure; `pause_source` could un-pause after restart; `StoreUnavailable` → 500 | **Fixed**: every mutator snapshot/rollback (test caught an aliasing bug in my first attempt), `StoreUnavailable` → 503 |
| 6 | Med | `install()` ran with flags off (DB connect, thread, per-event `deepcopy`) | **Fixed**: lazy store/thread, 2 s cached config read, tested. **Not fixed**: `_intelligence_section` uses last-good config, so a kill switch added while the central file is unreadable is ignored (should use `secure_section`). |
| 7 | Med | Gmail sender spoofing via display name; text-derived links auto-confirmed; label change = new event | **Fixed**: `parseaddr`, links are provisional (`inferred`, 0.9, needs review), `source_version` stable |
| 8 | Med | Artifact payload text could place events in any project | **Fixed**: only `ci_fix` streams resolve (from their validated first event) |
| 9 | Med-low | Active PDFs, late (>64 KiB) markers, OOXML externals/embeddings, zero-compress-size entries accepted | **Fixed**, tested. Still not an AV engine; zip triage reads header-declared sizes, so the real parser must enforce a streaming decompression budget. |
| 10 | Low-med | `evidence_open` window of 500; tombstone left derived links confirmed; dedupe swallowed lower-label edge; verified edges re-reviewable; pause org-wide by scoped viewer; concurrent append could lose an event | **Fixed** (by-id lookup, tombstone→provisional, dedupe key, review-state guard, org-wide-only pause, append under lock). **Not fixed**: findings citing a removed source are not re-evaluated; tombstone keeps `evidence`/`actor_ref`/`object_ref` metadata. |
| 11 | Low | JS refresh race / detached node | **Fixed**; new Playwright test, shown to fail against the old code (mutation check) |
| — | — | `ReplayGuard` is in-memory and recorded before processing | Added `seen()`/`mark()` (mark after durable store). Still in-memory: lost on restart; GitHub deliveries older than the process are not deduped by the guard (store dedupe still applies). |

Process note: while fixing, a `ruff check --fix` over the whole `control_plane/` directory auto-edited 8 files outside this change; all 8 were restored with `git checkout` before commit and `git status` was checked. Lint is now run on owned files only.

## Verification commands (exact)
```
python3 -m pytest tests/control_plane/test_project_intelligence.py tests/control_plane/test_project_intelligence_api.py \
  tests/control_plane/test_repair_policy.py tests/control_plane/test_intel_connectors.py tests/control_plane/test_intel_assistant_e2e.py -q   # 104 passed
python3 -m pytest tests/control_plane/test_project_intelligence_migration.py -q -m "integration or not integration"                           # 3 passed, private Postgres
python3 -m pytest tests/control_plane/test_api.py -q                                                                                         # 49 passed incl. new route tests
npm run test:client                                                                                                                          # 707 pass
node --test tests/playwright/project-intelligence.test.mjs                                                                                   # 6 pass (needs node_modules; PI_SHOTS=<dir> writes screenshots)
```
The full `tests/control_plane` suite did not finish inside a 560 s limit; it was not run to completion. Only the files above plus `test_api.py`, `test_db.py`, `test_events.py`, `test_artifact_registry_migration.py` were run.

## Usage (all off by default; YAML only)
```yaml
control_plane:
  project_intelligence:
    enabled: true            # master switch (404 when false); hot-read on every request
    ingest: true             # allow append_event        derive: true   # allow inferred relationships
    suggest: true            # allow review of inferences  execute: false  auto_merge: false   # stay off
    kill_switch: false       # blocks new ingestion immediately
    default_org_id: org1     # only for the open loopback listener's operator
    viewers:
      "key:<12-hex digest of the API key>": {org_id: org1, projects: [p-binbase], clearance: confidential}
```
Routes: `GET /api/v1/intelligence/{status,projects,investigations,relationships?ref=,findings}`, `GET .../projects/:id/timeline?source=&category=&actor=&as_of=&limit=`, operator-only `POST .../relationships/:id/review`, `POST .../sources/:system/pause|resume`.

## Divergences / limitations to know
- Viewer scope comes from a YAML key->scope map; the assistant's `X-Herdr-User` header is deliberately ignored (D3).
- `as_of` replays by *ingestion* time only; there is no entity-resolution replay.
- Event ids are deterministic UUIDv5 of the dedupe key; same key + different content raises Conflict (amendment required).
- Attachment triage is byte-level only: no antivirus engine, no libmagic. A real AV/parse sandbox with rlimits and no network is still required before untrusted files are parsed.
- This worktree's `node_modules` is a symlink to the main checkout's (gitignored); it is not part of the change.

## Completed in the second pass (2026-10-28)
- Bridge: durable store + fabric-event bridge done earlier; `ci_fix` events now fold into an in-memory `IncidentBoard` (`intel_incidents.py`).
- `intel_incidents.classify_failure`: infra / flaky / regression / unknown with a conservative action (`rerun`, `rerun_then_human`, `repair`, `human`).
- `repair_gate.py` wired into `GateService(repair_gate=...)`: inert unless `repair.enabled`; fail-closed when enabled (truncated or renamed
  file lists wait; bad config raises and the service turns it into `wait`; unreadable central config after enable rejects).
  Facts GitHub cannot prove (human approvals, signed provenance, reviewer context/model families) come from `facts_fn`; the default supplies none.
- Assistant: `read_project_intelligence` in `TOOL_CATALOG` (non-sensitive, needs a grant) scoped by YAML `assistant_viewers`.
- `llm_proxy`: `X-Herdr-Data-Class` tagged requests are gated by operator `egress` routes (`egress_denied`, HTTP 403); untagged traffic unchanged.
- Attachments: `intel_attachments.ingest` registers only accepted bytes through `ArtifactStore.register` (`temporary` retention).
- `POST /api/v1/intelligence/webhooks/github` (HMAC, replay guard, unmapped repos acknowledged not stored, 404 unless configured).
- `intel_gmail_sync.GmailSync` over an injected client: cursor, bounded backfill (dry-run default), deletion tombstones, consent revoke. Tested against a fake only.
- UI: related links (inferred shown as provisional with confidence) and findings (unverified labelled) in `/intelligence`.
- Operator metrics on `GET /api/v1/intelligence/status` (counters only). `RUNBOOK.md` with the config reference, enabling order, rollback and drills.

## Remaining work
1. Facts for the repair gate: a real `facts_fn` (reviewer execution context and model family from the review queue, human approvals from GitHub reviews, signed provenance), plus attempt and open-repair-PR counters. Until then repair PRs always wait for a human.
2. Webhook reconciliation (poll `open_prs` for missed deliveries); incident board persistence (it is rebuilt by backfill); `ci_fix` -> review-gate (`merged`, `post_merge_verified`) events are not emitted by any existing service yet.
3. Live Gmail client (B1); email sender and notification channel (B3); iPhone auth (B4).
4. UI: investigation picker, artifact provenance, graph and approval cards, an axe accessibility run.
5. Not fixed from review: findings citing a removed source are not re-evaluated; tombstones keep evidence metadata; ReplayGuard is in memory (a restart forgets deliveries; the store still dedupes); zip triage trusts header sizes; paging for hydration; `Idempotency-Key` header.
6. Manual audit and a staged rollout before any auto-merge enablement (B6).

## Blockers needing a user action
B1 Google OAuth client + consent · B2 GitHub App webhook secret file + public receiver URL · B3 transactional email channel · B4 iPhone device auth scheme / Rakazo compatibility · B5 API key to read live `/api/v1/health` · B6 any deploy, live-DB migration, or flag flip.

## Final verification (2026-10-28, after second pass)
- Control-plane + root + review-gate suites, 12 parallel shards: 7387 passed, 1 failed under load (`test_ci_monitor.py::test_unrelated_http_request_completes_while_provider_is_blocked`, a timing test; passes alone, 18/18), skips pre-existing. Integration on private Postgres: 13 pass.
- `npm test` UI contracts re-run for calm, dark-theme, skeletons, freshness-all and project-intelligence (9 tests): all pass except the same 3 that fail on clean origin/main; none involve `/intelligence`.
- Nothing pushed, merged or deployed. Acceptance criteria are NOT fully met: see Remaining work and blockers B1-B6.
