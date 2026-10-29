# Pi handoff: generic report artifacts, five-feature implementation

> Historical pause record. The user resumed on 2026-10-28; see
> [the current completion evidence](2026-10-28-generic-report-completion.md).
> The incomplete status and next steps below describe the original stop state.

## Stop state and objective

**User explicitly stopped Codex and requested this handoff. Implementation is
paused, incomplete, uncommitted, and not approved for deployment.** Do not mistake
component test results below for a final completion audit.

On a new user-authorized Pi continuation, finish all five generic features:

1. Machine-readable report descriptor linking questions, findings, evidence,
   figures, limitations, and next steps.
2. Meaningful version differences separating data, analysis/configuration,
   editorial, and presentation changes.
3. Distinct rendered / evidence-checked / reviewed states with attribution.
4. Feedback anchored to a finding, figure, or whole report on an exact immutable version.
5. Lightweight private previews and separate, explicit supporting-asset downloads.

Keep this **domain-neutral**. Do not copy LCB/ion/spectrum rules into the shared
artifact system. The prior LCB reports were motivation, not the framework schema.

## Repositories and safety

- Runtime implementation: `/home/wohlgemuth/IdeaProjects/herdr-engineering`.
- Canonical installed skill: `/home/wohlgemuth/IdeaProjects/herdr-artifacts`.
- Previous conversation/project: `/home/wohlgemuth/IdeaProjects/wcmc3`; do not
  continue artifact implementation in its pipeline modules.
- Runtime HEAD at handoff: `06e3d501ad95061a53157c05aa915c86d4482bfa`.
- Skill HEAD at handoff: `c6156eafea4e3552f915a3e7c6d5a7a86f12c3c0`.

The runtime checkout was already **very dirty before this work**: artifact
infrastructure, scheduler, collaboration, CLI, API, and dashboard changes were in
progress. Many foundational artifact files are untracked. Preserve all of it;
do not reset, clean, replace files from HEAD, or assume the whole diff is ours.
The local status/hash manifest beside the handoff package records the stop state.
Prefer narrow edits and targeted tests. A clean HEAD checkout alone lacks required
in-progress infrastructure.

No production services/databases were changed. Do not deploy, apply migrations to
production, expose/share artifacts, push, or merge without separate authorization.
Use the runtime's native migration path; this is not the unrelated WCMC Go schema.
Do not read or print credentials to reconstruct missing context. No real registry
artifact/version IDs were created for this feature; do not invent them.

## Design and implemented source

Read the plan first:
`docs/superpowers/plans/2026-10-26-generic-report-artifacts.md`.
Its checkboxes intentionally remain incomplete pending integration/security audit.

### Descriptor and semantic differences

- `herdr_engineering/control_plane/artifact_report_descriptor.py`
- `schemas/artifact-report-v1.schema.json`
- `tests/control_plane/test_artifact_report_descriptor.py`
- `tests/control_plane/fixtures/reports/{benchmark,incident-audit}.json`

Public helpers: `validate_report_descriptor(descriptor, output_manifest=None)`
returns a canonical deep copy; `report_diff(left, right)` classifies changes.
Descriptors are bounded at 512 KiB and contain stable-ID collections `questions`,
`evidence`, `findings`, `figures`, `assets`; optional `analysis`, limitations and
next steps. Qualitative evidence does not require numeric scores. Asset roles are
data/source/figure/result/preview; descriptors contain safe relative names,
SHA-256, MIME, size, and references, not credentials/URLs/raw large datasets.
Differences retain full counts but cap detail at 200 entries and bound old/new values.

### Durable checks, feedback, migration

- `herdr_engineering/control_plane/artifact_report_state.py`
- `deploy/control-plane/migrations/0008_artifact_reports.sql`
- `tests/control_plane/test_artifact_report_state.py`
- `tests/control_plane/test_artifact_report_postgres.py`

`ArtifactReportState(store)` provides inspect, record_check, add/list feedback,
and feedback_count. New append-only tables use composite artifact/version foreign
keys. Checks lock the pinned version before validating and appending. A later
upstream check makes older downstream signoff stale. Format validation seeds only
rendered, not evidence/review. Feedback validates exact descriptor target IDs.
The service supports bounded limit/offset pagination.

### Renderer, worker, adapter

- `artifact_report_render.py`: inert metadata-only preview, at most 256 KiB;
  explicit asset links, coverage/truncation notices, escaped text.
- `artifact_renderers.py`: descriptor-based report profile; legacy Markdown stays
  supported but retains profile `report`.
- `artifact_render_runner.py`: persists renderer validation, descriptor, source,
  manifests, existing asset references, worker/job provenance, and stable job key.
  **Latest security patch registers generated source, manifest, and preview blobs
  in the target artifact scope before committing.**
- `artifact_adapter.py`: serializes complete store operations with an event-loop
  lock so threaded HTTP calls do not overlap on one asyncpg connection.
- `worker.py`: atomic queued-job claim with UPDATE / SKIP LOCKED / RETURNING;
  actual assigned worker reaches renderer; no claim transaction spans rendering.

### Store, HTTP, CLI

- `artifacts.py`: optional immutable version descriptor and validation participate
  in CAS/idempotency; old non-report request hashes remain compatible. Artifact
  owner/grant checks; scoped upload records; report/check/feedback/diff operations;
  summary-only version history; DB-authoritative report-job status. Report commits
  now check **all report** source/output references, not just descriptor assets.
  Non-report-target cross-scope handling remains an open security issue below.
- `artifact_report_api.py`: native report create/build/status, per-version report,
  diff, checks, feedback, bounded preview and explicit verified asset download.
  Upload/download uses bounded chunks and temporary disk. Corrupt content is
  rejected before response bytes. Authors come from authentication.
- `api.py`: integration, narrow agent-role report routes, version/ACL/job permission
  guards, worker-owned report validation/context restrictions, not-found mapping.
- `artifact_reports_client.py` and `cli.py`: native
  `herdr-eng artifacts reports ...` command group. API key comes from an environment
  variable, not command-line values or URL. No redirect credential forwarding.

Commands currently implemented locally: create, upload-asset, build, status,
inspect, versions, diff, feedback, feedback-list, check, preview, download, validate.
Check `herdr-eng artifacts reports --help` for actual syntax.

### Dashboard

- `packages/artifacts-view.js`
- `packages/herdr-web-client/client.js`
- `packages/app.js` (route mounts `{ready, dispose}` before waiting)
- `packages/herdr-web-client/artifact-reports.test.js`
- `tests/playwright/artifact-reports{.test.mjs,-server.py}`

Version selector/deep link; descriptor navigation; categorized diff; independent
checks and forms; anchored feedback and pagination; lazy sandboxed preview; explicit
downloads; visible failures; stale-request cancellation. Late edits recognize
`version.is_report` from summary history and display diff truncation/count notices.
Those late edits require fresh verification after resumption. Current preview
fixture already has `get_report_descriptor`, but the HTTP preview is not yet wired
to use that lightweight method.

## Highest-priority unfinished work

1. **Close cross-artifact blob attachment through legacy/non-report targets.**
   The new report commit path checks scoped source/output references. However a
   caller can potentially create an `other` artifact and attach a known private
   report blob through the old globally-HEAD-verified output manifest, then
   materialize/share it. Any already artifact-scoped blob must require ownership
   or an authorized same-artifact reference regardless of target type. Preserve
   old truly unscoped blobs only with an explicit compatibility policy. Do not
   weaken this to merely checking descriptor assets. Add the adversarial regression.
2. **Recheck all legacy entrypoint bypasses.** Version reads/commits, ACL
   grant/revoke, generic job submit/read/cancel have been guarded in recent edits.
   Run the regressions again. Legacy review/publication routes still need review
   for report read/edit/review/share scope and exact artifact/version membership.
   Check browser-origin defenses on legacy mutation paths as well.
3. **Wire atomic report cancellation.** Store `cancel_report_job` exists, but the
   legacy cancel route still reads queued state and calls the in-memory manager;
   that check/update race can overwrite a worker transition. Use an atomic DB
   queued-only transition and DB-authoritative response, not stale manager state.
4. **Fix accepted-upload MIME consistency.** Upload currently accepts parameters
   such as `text/csv; charset=utf-8`; the descriptor allows bare MIME only. This
   can permanently register a scoped SHA with metadata no descriptor can bind.
   Normalize consistently or reject before uploading/registering, and test it.
5. **Finish bounded feedback/preview transport.** Store's initial report feedback
   was reduced to ten. `get_report_descriptor` exists. HTTP preview still calls
   `get_report` (thus fetching comments unnecessarily), and feedback route still
   defaults/allows 100. Large valid Unicode comments can exceed the client's 4 MiB
   JSON limit. Use descriptor-only preview and an explicitly bounded page (the
   intended next change was default ten / maximum twenty with accurate has_more).
6. **Finish canonical skill integration.** Earlier generic report guidance is in
   `herdr-artifacts`, but its wrapper still lacks a `report` route to the new native
   command group. Add that thin delegation, document implemented commands, and
   distribute/link the schema and small cross-domain examples. Do not copy domain
   analysis code into the skill. Preserve existing raw-upload aliases.
7. **Finish current-state verification.** The full native positive workflow passed
   before the newest all-output/source scope hardening. Re-run after policy fixes.
   Verify fixture/schema/examples, CLI, actual PostgreSQL, and actual browser together.
   Existing UI/header/source facts and screenshot evidence are useful but not a
   substitute for current end-to-end proof.

Possible extra integration issue to investigate rather than assume fixed:
existing job/review persistence callbacks pass manager float timestamps to
timestamptz fields and may need conversion; the native worker itself uses DB now().
Don't expand unrelated systems absent a reproducible effect on this workflow.

## Verification already observed (scope and timing matter)

- Descriptor/diff: **32 tests passed**, schema conformance and two unrelated domains.
- Checks/feedback: **17 tests passed**, including five actual isolated PostgreSQL
  tests, a physical restart, concurrent checks, append-only and cross-version constraints.
- Native integrated pipeline: **2 tests passed in 71.69 s** using actual PostgreSQL,
  Store/SyncAdapter/HTTP/worker/renderer/RustFSClient; only S3 transport is in memory.
  Four versions demonstrate editorial-only, data-only, and analysis-only changes;
  separate check states/feedback survive service reconnect; preview reads no asset
  bodies; explicit multi-MB download and corrupt-content rejection were verified.
  **This predates the newest all-reference scope hardening; re-run.**
- Worker: **7 tests passed**, including eight real-PostgreSQL claim contenders.
- Adapter: **3 tests passed**, including simultaneous threaded requests and failure lock release.
- Renderer/runner/jobs: **26 tests passed** after registering generated scoped blobs.
- Root store/API/client integration subsets had passing runs (32 backend/API tests
  and four client transport tests) before subsequent security patches; not a final suite.
- Browser agent reported seven earlier browser workflows plus mobile/desktop
  inspection, and 22 client tests. Late is_report/truncation fixes were interrupted
  by the user's stop and must be tested fresh.
- Screenshots, if still present: `/tmp/herdr-report-evidence/report-desktop.png`
  and `report-mobile.png`. These are local test evidence, not published artifacts.
- No deployment, production migrations, public share, push, or merge occurred.

The PostgreSQL tests use temporary Unix-socket clusters. `pg_config` can point at
an unavailable version 18; fixtures search installed 16/17 binaries. `--no-sync`
was added only to temporary initdb to avoid long startup sync. Don't restart a
confirmed live test just because an observation timed out; inspect its handle.

## Suggested continuation commands

Run from `/home/wohlgemuth/IdeaProjects/herdr-engineering`:

```sh
git status --short
python3 -m pytest -q tests/control_plane/test_artifact_report_descriptor.py tests/control_plane/test_artifact_report_store.py tests/control_plane/test_artifact_reports_api.py
python3 -m pytest -q tests/control_plane/test_artifact_report_state.py tests/control_plane/test_artifact_report_postgres.py tests/control_plane/test_report_job_worker.py tests/integration/test_generic_report_workflow.py
python3 -m pytest -q tests/test_artifact_reports_client.py tests/control_plane/test_artifact_render_runner.py tests/control_plane/test_artifact_report_render.py tests/control_plane/test_artifact_sync_adapter.py
node --test packages/herdr-web-client/client.test.js packages/herdr-web-client/artifact-reports.test.js
node --test --test-concurrency=1 tests/playwright/artifact-reports.test.mjs
herdr-eng artifacts reports --help
```

Then broaden appropriately to the existing artifact/API/worker/client suites,
run Ruff/compilation/syntax checks, and conduct an independent requirement audit.
Only claim the five features complete when their native runtime, permissions,
generic examples, skill wiring, and reader workflows are verified together.

## Pi startup and local handoff package

The user-facing Pi prompt is
`/home/wohlgemuth/IdeaProjects/wcmc3/PI-HERDR-REPORTS-RESUME.md`.
It records the local queued handoff package and claim/accept/start instructions.
No Pi session was automatically launched or synchronized to a live control plane.
Use the user's configured Pi model; no model override was requested.

`herdr-handoff` is not currently on PATH. Its SDK/CLI source is available at
`/home/wohlgemuth/IdeaProjects/herdr-handoff/src/herdr_handoff_cli.py`; use Python
to invoke it without installing or modifying another project.
