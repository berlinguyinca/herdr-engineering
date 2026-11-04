# Generic report artifacts: local completion evidence

The user resumed the paused task on 2026-10-28. This record supersedes the
2026-10-26 pause/handoff for implementation status. Changes remain uncommitted;
no deployment, production migration, public sharing, push or merge was performed.

## Requirements and implementation

| Requirement | Implementation and proof |
| --- | --- |
| Generic descriptor | Runtime validator and v1 schema link stable questions, evidence, findings, figures and separate assets; qualitative audit and benchmark examples validate. |
| Meaningful differences | Data, analysis, editorial and presentation categories retain complete counts with bounded details and omission notices. Unit, native pipeline and reader checks pass. |
| Independent check stages | Append-only PostgreSQL events attribute rendered, evidence_checked and reviewed separately; prerequisites, stale signoff, restart and concurrency tests pass. Rendering alone never verifies evidence. |
| Version-pinned feedback | Findings, figures and whole-report comments validate exact immutable version targets; bounded pages and reader navigation pass. |
| Lightweight private reader | Descriptor-only preview does not read supporting blobs; explicit downloads verify pinned size/digest before delivery. Mixed/legacy histories use authorized version-pinned outputs. Desktop/mobile layouts inspected. |

The canonical skill lives in `../herdr-artifacts/skill/herdr-artifact/`.
Its `scripts/herdr-artifact report ...` wrapper delegates to the native
`herdr-eng artifacts reports` group. See its `references/report-commands.md`
for actual commands, schema, synthetic examples and interpretation guidance.
Domain-specific calculations remain outside this framework.

## Security and compatibility fixes after resuming

- Scoped private blobs cannot be attached through legacy/non-report source or
  output references, forged digest metadata, or a historical fabric registration.
  Independently uploaded same-artifact copies and truly unscoped historical blobs
  remain compatible. Actual PostgreSQL regression coverage is included.
- Output downloads derive content identity from the stored blob reference and
  reject conflicting digest fields or a missing reference before reading bytes;
  adversarial HTTP regressions cover substitution attempts.
- Legacy reviews/publication operations enforce permissions and exact version
  membership; mutation routes check browser origin.
- Cancellation uses an atomic queued-only database transition.
- Parameterized upload MIME types are rejected before storage; descriptors and
  upload registrations therefore share bare MIME metadata.
- Feedback defaults to ten entries, maximum twenty; CLI and dashboard agree.
- Embedded previews retain their sandbox, remove unusable embedded links,
  and direct readers to authenticated controls outside the frame.

## Fresh verification

Run from the runtime repository unless otherwise stated:

- Descriptor/state/PostgreSQL/preview/adapter/worker/ACL/reviews/jobs/renderers/
  streaming/client group: **84 passed**.
- Store/version/runner group after digest agreement hardening: **35 passed**.
- Report API/legacy artifact API/general API/native workflow group: **71 passed**.
  After the final output-reference hardening, report API and native workflow
  tests passed again: **20 passed**. Counts overlap and are not a unique-test total.
- Browser report workflows: **10 passed**, including mixed and all-legacy history,
  immutable feedback, separate checks, lazy downloads, error handling and mobile.
- JavaScript client tests: **23 passed**.
- Canonical skill `python3 -m pytest -q tests/test_reports.py`: **4 passed** plus
  four subtests; real wrapper/native validation, example asset hashes, schema.
- Targeted Ruff, JavaScript syntax, shell syntax, schema byte comparison and
  tracked-file whitespace checks passed.
- Independent review approved the repaired legacy reader, embedded previews,
  output-reference agreement and download authorization; no remaining material
  findings in that scoped re-review.

The native integration uses temporary PostgreSQL, actual Store/SyncAdapter/HTTP/
worker/renderer/RustFSClient, and an in-memory S3 transport. This proves the local
workflow, not connectivity to a deployed RustFS/control plane. No production
migration was applied. LSP provides no Python/typecheck evidence in this checkout;
Ruff, syntax checks and runtime tests are the relevant checks performed.

Fresh inspected screenshots: `/tmp/herdr-report-evidence/report-desktop.png` and
`/tmp/herdr-report-evidence/report-mobile.png`. These are local test fixtures,
including intentionally escaped hostile title text, not production report data.

The checkout contains extensive unrelated pre-existing changes. Preserve them;
this completion record does not authorize resetting, committing or deploying
the entire working tree.
