# Artifact share panel and merge engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship plan 1 (Share panel GUI with link tracking, history view, "Restore this version") and plan 2 (version parent graph + three-way text merge engine) of the artifact sharing / collaboration design.

**Architecture:** Plan 1 is front end only: new web-client methods over the already-merged share routes, a `artifact-share-panel.js` component mounted as a card on the artifact detail page, and a restore action that re-commits an earlier version's manifests through the existing `POST /api/v1/artifacts/{id}/versions`. Plan 2 is backend only: a pure `artifact_merge.py` (diff3 text merge + manifest merge), migration `0042` adding a real parent list to versions, `ArtifactStore.commit_version` taking optional parents, and a store-level `merge_commit` that the later collaboration bridge (plan 3) will call.

**Tech Stack:** Python 3.12 / asyncpg (control plane), plain ES-module JS web client (`packages/`), node:test for client unit tests, Playwright for UI tests, Postgres migrations in `deploy/control-plane/migrations`.

**Spec:** `docs/superpowers/specs/2026-10-30-artifact-sharing-gui-and-collaboration-design.md` (sections A, C and "History model"; the merge rules under B). Share routes: `docs/superpowers/specs/2026-10-30-artifact-share-links-design.md`, code `herdr_engineering/control_plane/artifact_share_api.py`.

## Global Constraints

* Never print or log share tokens/URLs except where the UI shows the approve URL once; never store it in localStorage/sessionStorage/URLs/cookies.
* All dynamic text goes through `esc()` (`packages/herdr-web-components/util.js`); no `innerHTML` with unescaped data; no inline event handlers.
* Share routes: list `GET /api/v1/artifacts/{id}/shares`, create `POST …/shares` (fields `version_id, audience (lab|public), allow_emails, allow_groups, expires_in_days, entry, title`; returns 201 `{share, summary}`), show `GET …/shares/{sid}`, approve `POST …/shares/{sid}/approve` body `{"confirm": true}` returns `{share, url, summary}`, revoke `POST …/shares/{sid}/revoke`, events `GET …/shares/{sid}/events`. Public: `expires_in_days` required, 1–90 (default 30). Lab: optional, 1–3650. Members may never create `public` (that is plan 5; do not add any member UI here).
* Merge rules (spec B): per file name; one side changed → take it; identical change → take it; delete vs unchanged → delete; add/add different, delete vs modify → conflict; both modified text (UTF-8, ≤ 2 MiB) → line-based diff3, non-overlapping hunks merge, overlapping → conflict with `<<<<<<< / ======= / >>>>>>>` markers; non-UTF-8/oversize/binary → conflict. No external `git` binary.
* History: `parent_version_id` stays the first parent; add `parent_version_ids text[]`. A rejected attempt is logged in `artifact_collab_attempts`, never stored as a version. Nothing ever overwrites history.
* Python: type hints, matches surrounding style (`from __future__ import annotations`, asyncpg conn passed to store, `ArtifactError`/`ArtifactConflict`). JS: ES modules, 2-space indent, JSDoc-free, matching `artifacts-view.js`.
* Commit trailer on every commit: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

* Hostile server/user strings in the panel (title, emails, entry, file names, warnings, error text) never become markup. Test with `<img onerror>` and `"><script>` values.
* The approve URL is visible only in the one dialog that received it; closing the panel or re-rendering the card removes it from the DOM, and a failed/double click on Approve never creates a second link or shows a stale one.
* A double-click, retry or network error on Create/Approve/Revoke leaves the UI consistent (buttons disabled while in flight, error shown, list refreshed from the server).
* Restore never targets a report artifact (descriptor required) and never commits against a stale head (409 shows a clear "head moved, reload" message).
* Merge engine: identical edits on both sides, edits at the same insertion point, trailing-newline and CRLF differences, empty base, binary/non-UTF-8 files, files differing only by case in name, very long lines — none may crash or silently drop content.
* Migration is additive and idempotent; existing code that reads `parent_version_id` keeps working; a version created before the migration has `parent_version_ids == [parent_version_id]`.

---

## Plan 1 — Share panel GUI

### Task 1: Web-client share and history methods

**Files:** Modify `packages/herdr-web-client/client.js`; Create `packages/herdr-web-client/artifact-shares.test.js`

**Interfaces:**
- Produces (methods on the existing client class, all `{ signal } = {}` last arg, same `request` helper): `listArtifactShares(id)`, `createArtifactShare(id, body)`, `getArtifactShare(id, shareId)`, `approveArtifactShare(id, shareId)` (sends `{confirm: true}`), `revokeArtifactShare(id, shareId)`, `artifactShareEvents(id, shareId)`, `restoreArtifactVersion(id, versionId)`.
- `restoreArtifactVersion` reads the head via `getArtifact(id)` (`current_version_id`) and the target via `artifactVersion(id, versionId)`, then POSTs `{base_version_id: head, source_manifest_blob, output_manifest, provenance: {restored_from: versionId}, idempotency_key: "restore-<versionId>-<head>"}` to `/api/v1/artifacts/{id}/versions`; it throws an `Error` with `.code === "restore_unsupported"` if the target version has `report_descriptor` or the artifact is a report, and lets a 409 propagate (error carries `.status === 409`).

- [ ] **Step 1: Write failing tests** in `artifact-shares.test.js` using the fake `fetchImpl` pattern of `artifact-reports.test.js`: each method hits the exact path/method/body listed above; approve sends `{"confirm":true}`; ids are `encodeURIComponent`-ed; restore sends the exact body above, refuses a report artifact with `restore_unsupported`, propagates a 409.
- [ ] **Step 2: Run** `node --test packages/herdr-web-client/artifact-shares.test.js` → FAIL (methods missing).
- [ ] **Step 3: Implement** the methods in `client.js` next to the existing artifact methods.
- [ ] **Step 4: Run** the new test plus `npm run test:client` → PASS.
- [ ] **Step 5: Commit** `feat(web): client methods for artifact shares and restore`.

### Task 2: Share panel component (viewer links)

**Files:** Create `packages/artifact-share-panel.js`; Modify `packages/artifacts-view.js` (mount a "Share" card in `renderArtifactDetail` and in `renderReportReader`'s page); Test `packages/artifact-share-panel.test.js` (node, jsdom-free: check how existing `packages/*.test.js` render — reuse the same DOM test helper those tests use; if none exists, test pure render functions returning HTML strings).

**Interfaces:**
- Consumes: Task 1 client methods; `esc` from `herdr-web-components/util.js`; the version list already fetched by the detail page.
- Produces: `export function mountSharePanel(container, client, artifactId, { versions, headVersionId, signal })` returning `{ dispose() }`; pure helpers `renderShareForm(state)`, `renderShareRow(share, eventsSummary)`, `renderSummary(summary)` (file list, total size, warnings) each returning an HTML string built only from escaped values.

- [ ] **Step 1: Write failing tests** for the pure renderers: hostile strings are escaped (`<img onerror>`); public form shows the expiry field (default 30, min 1, max 90) and no lab fields; lab form shows email/group fields (comma/newline separated) and an optional expiry; private audience selection shows "No link is created" and disables Generate; summary lists each file name and size and every warning; a revoked/expired row has no Revoke button; row shows audience, recipients, state, expires, opened/denied counts.
- [ ] **Step 2: Run** the test → FAIL.
- [ ] **Step 3: Implement** the renderers and `mountSharePanel`. Add `packages/*.test.js` to the globs of the `test` and `test:client` scripts in `package.json` so the new tests (and Task 3's) run in CI (first check no existing `packages/*.test.js` file breaks under it). Flow: load `listArtifactShares` (per-section error with `<herdr-error>` + retry like the other sections); form → **Review** calls `createArtifactShare` and shows the returned summary in a confirm region; **Generate link** calls `approveArtifactShare`, then shows the URL once in a dialog region with a Copy button (`navigator.clipboard`, fall back to select-all) and the text "This link cannot be shown again"; closing the region clears the URL from the DOM and refreshes the list. Buttons are disabled while a call is in flight; any error shows the server `error` text escaped and refreshes the list. Revoke asks `confirm()` first. Each row has an Events toggle that lazily loads `artifactShareEvents` and shows opened/denied/created/revoked lines. No data in storage/URLs. Audience labels in the UI: Private / Group (lab) / Public.
- [ ] **Step 4: Mount** the panel from `renderArtifactDetail` (after the Versions card) and the report reader, passing `versions` and the head version id; dispose it when the route changes.
- [ ] **Step 5: Run** the unit tests and `npm run test:client` → PASS. **Commit** `feat(web): artifact share panel for viewer links`.

### Task 3: History card with parents and "Restore this version"

**Files:** Modify `packages/artifacts-view.js` (the Versions card), Modify `packages/artifact-share-panel.js` only if shared helpers are needed; Test `packages/artifact-history.test.js`.

**Interfaces:**
- Consumes: `restoreArtifactVersion` (Task 1); version summary fields `version_number, version_id, state, created_by, created_at, parent_version_id` and, once plan 2 ships, `parent_version_ids` (render parents when present, fall back to `parent_version_id`).
- Produces: `renderVersionRow(version, { headVersionId, canRestore })` returning escaped HTML: version number, author (`created_by`), time, parents (a merge is marked when ≥ 2 parents), a "current" badge on the head, and a **Restore this version** button on non-head versions only when `canRestore` (non-report artifacts).

- [ ] **Step 1: Write failing tests**: head has no Restore button; non-head has one; report artifacts never show it; merge versions show both parents; hostile `created_by` is escaped; restore handler disables the button, calls the client, on success re-renders the list and shows "Restored as vN", on 409 shows "The head moved — reload and try again".
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** and wire into the Versions card replacing the plain `<li>` rendering (keep the existing `data-artifact-versions` hook and empty/error states). **Step 4: Run** `npm run test:client` → PASS. **Step 5: Commit** `feat(web): version history with parents and restore`.

### Task 4: Playwright end-to-end against a stub share server

**Files:** Create `tests/playwright/artifact-shares-server.py`, `tests/playwright/artifact-shares.test.mjs`; Modify `package.json` only if a new script entry is needed (follow how `artifact-reports.test.mjs` is wired); possibly update visual baselines under the ui-gates convention if the artifact detail page is a covered surface.

**Interfaces:** the stub server is a small stdlib HTTP server (`PORT=n` printed on stdout like `artifact-reports-server.py`) serving the dashboard static files plus an in-memory implementation of the artifact routes the page needs (`GET artifact`, `GET versions`, `GET/POST shares…`, `POST versions`), with the exact response shapes in Global Constraints.

- [ ] **Step 1: Write the Playwright test**: open an artifact detail page; Private selected → Generate disabled; choose Public, set expiry 7, Review → summary shows files; Generate → dialog shows a URL with the stub's host; Copy works; close → URL no longer in the DOM (`page.content()` does not contain the token); list shows one active public row; double-click Generate does not create a second share (server counts one approve); Revoke → row shows revoked; hostile title `"><img src=x onerror=window.__x=1>` does not execute (`window.__x` stays undefined); Restore on a non-head version creates a new head and the list shows it, 409 path shows the message.
- [ ] **Step 2: Run** (`npx playwright`/the repo's `npm run test:ui` for this file) → FAIL, then implement the stub until PASS (no product code expected to change here; fix product code if the test reveals a defect).
- [ ] **Step 3: Run the existing UI gates for the touched surface** (`npm run test:ui-gates` or the repo's equivalent as used in CI) and update any intentionally changed baselines following the repo's baseline-update procedure; note the dashboard-missions__mobile baseline is already red on main (1615 vs 1610) and is NOT yours to touch.
- [ ] **Step 4: Commit** `test(web): share panel end-to-end`.

---

## Plan 2 — History model and merge engine

### Task 5: Three-way text merge

**Files:** Create `herdr_engineering/control_plane/artifact_merge.py`; Test `tests/control_plane/test_artifact_merge_text.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class TextMerge: text: str; conflicted: bool; conflicts: int`; `def merge3_text(base: str, ours: str, theirs: str, *, ours_label: str = "head", theirs_label: str = "mine") -> TextMerge` — line-based diff3 built on `difflib.SequenceMatcher` (autojunk off), preserving each side's line endings and trailing-newline state; conflicted output uses `<<<<<<< {ours_label}` / `=======` / `>>>>>>> {theirs_label}` markers.

- [ ] **Step 1: Write failing tests**: non-overlapping edits merge (top vs bottom); identical edits on both sides merge once; insertion at the same point by both with different text → conflict; one side unchanged → other side; both unchanged; empty base with different additions → conflict, identical additions → merged; delete vs edit of the same line → conflict; adjacent-but-not-overlapping hunks merge; CRLF files keep CRLF; missing trailing newline on one side preserved; output for a conflict contains exactly the markers and both sides' lines; 5,000-line file with 50 scattered edits per side merges quickly (< 2 s) and without conflicts; property test (hypothesis if already a dev dependency, else a seeded random loop): merging `(base, base, x)` and `(base, x, base)` returns `x`, and merging `(base, x, x)` returns `x`.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_artifact_merge_text.py -q` → FAIL.
- [ ] **Step 3: Implement** `merge3_text`. Approach: split with `keepends=True`, compute matching blocks base↔ours and base↔theirs, walk base regions classifying each as stable / ours-changed / theirs-changed / both (identical → take; different → conflict), emit with markers.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(artifacts): three-way text merge`.

### Task 6: Manifest merge

**Files:** Modify `herdr_engineering/control_plane/artifact_merge.py`; Test `tests/control_plane/test_artifact_merge_manifest.py`

**Interfaces:**
- Consumes: `merge3_text` (Task 5); manifests are lists of dicts with at least `name`, `sha256`, `size_bytes`, `media_type` (optional) and a storage reference.
- Produces: `@dataclass(frozen=True) class FileConflict: name: str; kind: str; base_sha256: str | None; theirs_sha256: str | None; mine_sha256: str | None; merged_text: str | None` with `kind` in `add_add`, `delete_modify`, `modify_modify_text`, `binary`, `oversize`, `not_utf8`, `case_collision`; `@dataclass(frozen=True) class ManifestMerge: files: list[dict]; merged_names: list[str]; clean_names: list[str]; conflicts: list[FileConflict]`; `def merge_manifests(base: list[dict], head: list[dict], mine: list[dict], *, read_text: Callable[[dict], str], store_text: Callable[[str, dict], dict], max_text_bytes: int = 2 * 1024 * 1024) -> ManifestMerge`. `read_text(entry)` returns decoded UTF-8 text (raising `UnicodeDecodeError` is treated as `not_utf8`); `store_text(name, template_entry)` stores merged text and returns a complete manifest entry (new sha256/size/storage reference). If any conflict exists, `files` is empty and nothing is stored.

- [ ] **Step 1: Write failing tests** covering every rule in Global Constraints and each `kind`, including: file added on one side only; deleted on one side and unchanged on the other; both sides delete; identical modification; modify/modify non-overlapping → merged text stored once via `store_text` and listed in `merged_names`; names differing only by case → `case_collision`; a text file over `max_text_bytes` → `oversize`; media type not text-like (`text/*`, `application/json|xml|yaml|javascript`, or names ending `.md .html .css .js .json .yaml .yml .xml .txt .csv .svg .py .ts`) → `binary`; result ordering is stable (sorted by name); `store_text` is never called when any conflict exists.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat(artifacts): three-way manifest merge`.

### Task 7: Migration 0042 and parent-aware commits

**Files:** Create `deploy/control-plane/migrations/0042_artifact_version_parents.sql`; Modify `herdr_engineering/control_plane/artifacts.py` (`commit_version`, `get_version`, `list_versions`, `list_version_summaries` — wherever version rows are shaped); Test `tests/control_plane/test_artifact_version_parents.py` (use the Postgres fixtures the other `test_artifact_*postgres*` tests use)

**Interfaces:**
- Migration (additive, idempotent, `IF NOT EXISTS`): `artifact_versions` gains `parent_version_ids text[] NOT NULL DEFAULT '{}'`, `created_via text NOT NULL DEFAULT 'owner' CHECK (created_via IN ('owner','collaborator'))`, `collaborator_agent_id text NULL`; backfill `parent_version_ids = ARRAY[parent_version_id]` where `parent_version_id IS NOT NULL AND parent_version_ids = '{}'`; create `artifact_collab_attempts(attempt_id text PK, artifact_id text NOT NULL REFERENCES artifacts, collaborator_agent_id text NOT NULL, base_version_id text, head_version_id text, conflict_names jsonb NOT NULL DEFAULT '[]', message text, created_at timestamptz NOT NULL DEFAULT now())` with an index on `(artifact_id, created_at DESC)`.
- `commit_version(..., parent_version_ids: Sequence[str] | None = None, created_via: str = "owner", collaborator_agent_id: str | None = None)`: when `parent_version_ids` is None it is `[base_version_id]` (or `[]` for the first version); every id must belong to the same artifact (else `ArtifactError`); `parent_version_id` and `base_version_id` stay the first/`base` as today; returned/shaped rows include `parent_version_ids`, `created_via`, `collaborator_agent_id`. The idempotency request hash includes the parents.

- [ ] **Step 1: Write failing tests**: after the migration, a pre-existing version has `parent_version_ids == [parent]`; linear commit records `[base]`; commit with two parents stores both in order and `parent_version_id` equals the first; a parent from another artifact is refused; `created_via="collaborator"` requires an agent id; migration runs twice without error; existing artifact tests still pass (run `tests/control_plane/test_artifact_versions.py` and `test_artifacts.py`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** migration and store changes (and the version-row shaping used by the HTTP summaries so the web client sees `parent_version_ids`). **Step 4: Run** new + existing artifact tests → PASS. **Step 5: Commit** `feat(artifacts): version parent lists and attempt log`.

### Task 8: Merge commit in the store and per-parent lineage

**Files:** Modify `herdr_engineering/control_plane/artifacts.py` (new `merge_commit`), `herdr_engineering/control_plane/artifact_lineage.py` (`emit_version_lineage`); Test `tests/control_plane/test_artifact_merge_commit.py`, extend the lineage tests

**Interfaces:**
- Consumes: `merge_manifests` (Task 6), parent-aware `commit_version` (Task 7).
- Produces: `async def merge_commit(self, artifact_id: str, *, base_version_id: str, changes: list[dict], read_text, store_text, message: str, created_by: str, collaborator_agent_id: str, idempotency_key: str | None = None, max_text_bytes: int = 2 * 1024 * 1024) -> dict` where `changes` items are `{name, op: "put"|"delete", entry?: dict}` applied on top of the base manifest to form *mine*; returns `{"result": "created"|"merged", "version": <version dict>, "merged_names": [...]}` or raises `ArtifactMergeConflict(head_version_id, conflicts: list[FileConflict], clean_names)` after logging one `artifact_collab_attempts` row; `created` when `base == head` (fast-forward, parents `[head]`), `merged` when `base != head` and clean (parents `[head, base]`). It refuses `report` artifacts and artifacts with a renderer source with `ArtifactError("not writable by collaboration")`, honours a retracted artifact (409-equivalent error as today), takes the same artifact row lock as `commit_version`, and is idempotent per `(created_by, idempotency_key)`. `emit_version_lineage` emits a supersession edge for every id in `parent_version_ids` (not only the first).

- [ ] **Step 1: Write failing tests** with in-memory `read_text`/`store_text` fakes against the Postgres fixtures: fast-forward → `created`, parents `[head]`; stale base with non-overlapping edits → `merged`, parents `[head, base]`, merged file text contains both edits; stale base with overlapping edits → `ArtifactMergeConflict` carrying markers, **no version stored, head unchanged, one attempts row**; delete-vs-modify conflict; replay with the same idempotency key returns the same version; concurrent identical calls (two tasks) produce one version; report artifact refused; retracted artifact refused; lineage test: a merge version emits two edges.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** the new tests plus `test_artifact_lineage.py`, `test_artifact_render_lineage.py`, `test_artifact_versions.py` → PASS. **Step 5: Commit** `feat(artifacts): merge commits with per-parent lineage`.

---

## Self-review notes

* Spec coverage: section A (panel, tracking, one-time URL, revoke, events) → Tasks 1–2, 4; "History view" and "Restore" → Task 3; history model + merge rules + attempts log + lineage → Tasks 5–8. Not in this plan (later plans): collaborators/mirror/intents/MCP tools (plan 3), annotations (4), member artifacts (5), the "Activity" tab's collaborator list and rejected-attempts toggle (plan 3, once attempts exist).
* Names used across tasks: `merge3_text`, `merge_manifests`, `FileConflict`, `ManifestMerge`, `ArtifactMergeConflict`, `merge_commit`, `parent_version_ids`, `created_via`, `collaborator_agent_id`, `artifact_collab_attempts` — consistent.
* The detail page needs `parent_version_ids` in version summaries for Task 3's merge display; Task 3 falls back to `parent_version_id` until Task 7 ships, so the plans are independently shippable.
