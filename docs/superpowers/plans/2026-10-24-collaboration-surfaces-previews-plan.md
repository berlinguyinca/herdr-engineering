# Collaboration Surfaces and Previews Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make public collaboration and the Herdr dashboard expose the same rooms, permissions, files, uploads, previews, and status states, with safe format-specific previews.

**Architecture:** Keep shared route/state logic in the existing collaboration client and render public/dashboard shells around it. Add a room file browser backed by metadata and short-lived authorized object access. Implement preview adapters behind a registry; expensive previews are asynchronous room-owned artifacts.

**Tech Stack:** Existing `packages/collab-*`, `packages/herdr-web-client`, web components, browser APIs, PDF renderer, syntax highlighter/tree renderer, Flasheic adapter, Node tests, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-24-collaboration-rooms-data-platform-design.md`

## Global Constraints

- Every public capability must also be available in the dashboard against the same source of truth.
- Use “Remove from room” and “Delete person permanently” as separate labels and confirmation flows.
- Preview all untrusted content in a sandbox; never execute scripts or active XML content.
- Keep file bytes behind room authorization and short-lived URLs/range access.

### Task 1: Extract shared capability/state model from the collaboration client

**Files:**
- Modify: `packages/herdr-web-client/collab.js`
- Modify: `packages/collab-view.js`
- Modify: `packages/collab-app.js`
- Test: `packages/herdr-web-client/collab.test.js`
- Test: `packages/herdr-web-client/collab-flow.test.js`

**Interfaces:**
- `CollabApi.me()`
- `CollabApi.rooms()`
- `CollabApi.roomFiles(roomId, cursor)`
- `CollabApi.uploadStatus(uploadId)`
- `CollabApi.skills(scope)`
- shared route state `{ surface, route, roomId, status }`.

- [ ] **Step 1: Add failing client tests** proving public and dashboard clients consume identical response shapes.
- [ ] **Step 2: Run `npm test -- --test-name-pattern='collab|roomFiles|uploadStatus'` and verify failure.**
- [ ] **Step 3: Add the shared API methods and normalized status/error handling.**
- [ ] **Step 4: Refactor standalone/dashboard routing to consume shared room and capability models.**
- [ ] **Step 5: Run `npm test` and commit** `git commit -m "refactor: share collaboration capability client"`.

### Task 2: Build People and room administration parity

**Files:**
- Modify: `packages/collab-view.js`
- Modify: `packages/collab.css`
- Modify: `packages/collab.html`
- Test: `packages/herdr-web-client/collab.test.js`
- Test: `tests/playwright/collaboration.test.mjs`

**Interfaces:**
- `renderPeople(model)`
- `renderRoomAdministration(model)`
- `confirmDeletePerson(person)`
- `confirmDeleteRoom(room)`

- [ ] **Step 1: Add failing UI tests** for distinct remove/delete actions, pending approvals, upload permission toggles, agent request prompts, archive progress, and public/dashboard parity.
- [ ] **Step 2: Run the focused Node/Playwright tests and verify failure.**
- [ ] **Step 3: Implement People view and room-owner controls using existing shell/card/modal patterns.**
- [ ] **Step 4: Add destructive confirmations that spell out retention effects and block duplicate submissions.**
- [ ] **Step 5: Run `npm run test:client` and `npm run test:ui -- --grep collaboration`.**
- [ ] **Step 6: Commit** `git commit -m "feat: add collaboration administration parity"`.

### Task 3: Add room file browser and upload status

**Files:**
- Create: `packages/collab-files.js`
- Modify: `packages/collab-view.js`
- Modify: `packages/collab.css`
- Test: `packages/herdr-web-client/collab-files.test.js`
- Test: `tests/playwright/collaboration-files.test.mjs`

**Interfaces:**
- `renderFileBrowser(files, state)`
- `renderUploadBatch(batch)`
- `openAuthorizedFile(file)`
- `startBulkUploadPreview(roomId, pattern)`.

- [ ] **Step 1: Write failing tests** for path navigation, search, pending/failed/available states, authorized download, upload progress, and hidden-until-finalized files.
- [ ] **Step 2: Run focused tests and verify failure.**
- [ ] **Step 3: Implement file browser and aggregate upload progress without per-file DOM/event churn.**
- [ ] **Step 4: Add room-owner/member file permission controls.**
- [ ] **Step 5: Run client and Playwright tests.**
- [ ] **Step 6: Commit** `git commit -m "feat: add collaboration room file browser"`.

### Task 4: Add preview registry and safe renderers

**Files:**
- Create: `packages/collab-previews.js`
- Create: `packages/collab-preview-msp.js`
- Create: `packages/collab-preview-text.js`
- Create: `packages/collab-preview-pdf.js`
- Create: `packages/collab-preview-flasheic.js`
- Modify: `packages/collab-view.js`
- Test: `packages/herdr-web-client/collab-previews.test.js`
- Test: `tests/playwright/collaboration-previews.test.mjs`

**Interfaces:**
- `previewRendererFor(file) -> PreviewRenderer`
- `renderPreview(file, authorizedSource) -> HTMLElement`
- `requestAsyncPreview(file) -> PreviewJob`

- [ ] **Step 1: Add failing renderer-selection and sanitization tests** for MSP, chromatogram, PDF, Markdown, JSON, XML, YAML, and unsupported files.
- [ ] **Step 2: Run `npm test -- --test-name-pattern='preview'` and verify failure.**
- [ ] **Step 3: Implement safe text/tree renderers and MSP spectrum plotting with bounded input size.**
- [ ] **Step 4: Implement PDF rendering and Flasheic adapter using authorized file references, not public object URLs.**
- [ ] **Step 5: Add asynchronous preview polling and derived-artifact display.**
- [ ] **Step 6: Run browser tests with hostile Markdown/XML fixtures and commit** `git commit -m "feat: add safe collaboration file previews"`.
