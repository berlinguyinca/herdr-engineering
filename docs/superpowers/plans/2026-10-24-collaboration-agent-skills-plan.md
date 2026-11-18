# Collaboration Agent Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users refine agent-driven analyses in chat, save reproducible outputs as versioned skills, publish them at private/room/shared scope, and let authorized agents download and install them locally.

**Architecture:** Add a skill registry beside collaboration state, with immutable content-addressed versions and explicit capability scopes. Run generated tools in the registered harness, not in the control plane, and store only manifests, provenance, outputs, and packages through the shared storage service.

**Tech Stack:** Python collaboration service, Postgres metadata, S3-compatible package/artifact storage, MCP tools, existing harness execution/sandbox conventions, pytest, Node client tests.

**Spec:** `docs/superpowers/specs/2026-10-24-collaboration-rooms-data-platform-design.md`

## Global Constraints

- Generated tooling is untrusted until validated and sandboxed.
- Private skills are available only to the creator’s agent; room skills require room authorization; shared skills require administrators/designated maintainers.
- Skill execution cannot expand room or file access.
- Packages are immutable, versioned, content-addressed, and checksum/signature verified.
- Analysis outputs retain file IDs, skill version, parameters, runtime, and timestamps.

### Task 1: Add skill registry schema and package manifest

**Files:**
- Modify: `deploy/control-plane/migrations/0003_collaboration_data_platform.sql`
- Create: `herdr_engineering/control_plane/skill_registry.py`
- Test: `tests/control_plane/test_skill_registry.py`

**Interfaces:**
- `create_skill_draft(principal, metadata) -> SkillDraft`
- `publish_skill(principal, draft_id, scope) -> SkillVersion`
- `get_skill_version(skill_id, version) -> SkillPackage`
- `verify_skill_package(package) -> VerificationReport`

- [ ] **Step 1: Write failing tests** for manifest fields, immutable versions, private/room/shared authorization, and checksum mismatch.
- [ ] **Step 2: Run `pytest -q tests/control_plane/test_skill_registry.py` and verify failure.**
- [ ] **Step 3: Add `skills`, `skill_versions`, and installation/audit tables with immutable version constraints.**
- [ ] **Step 4: Implement canonical package manifest containing `SKILL.md`, schemas, capabilities, fixtures/tests, dependency lock, provenance, and signature/hash.**
- [ ] **Step 5: Run focused tests and commit** `git commit -m "feat: add versioned collaboration skill registry"`.

### Task 2: Add chat-driven analysis artifact provenance

**Files:**
- Create: `herdr_engineering/control_plane/collab_analysis.py`
- Modify: `herdr_engineering/control_plane/collab.py`
- Modify: `herdr_engineering/control_plane/collab_mcp.py`
- Test: `tests/control_plane/test_collab_analysis.py`

**Interfaces:**
- `create_analysis_request(principal, room_id, prompt, file_ids) -> AnalysisRequest`
- `record_analysis_output(request_id, output_manifest) -> AnalysisArtifact`
- `promote_analysis_to_skill(principal, artifact_id, scope, metadata) -> SkillDraft`

- [ ] **Step 1: Write failing tests** for authorized file resolution, provenance, chat refinement, output artifact creation, and promotion to draft.
- [ ] **Step 2: Run focused tests and verify failure.**
- [ ] **Step 3: Implement analysis request/output records and MCP envelopes that contain only authorized file references.**
- [ ] **Step 4: Add explicit user promotion from analysis output to skill draft.**
- [ ] **Step 5: Run tests and commit** `git commit -m "feat: preserve provenance for chat analyses"`.

### Task 3: Add skill search/download/install MCP operations

**Files:**
- Modify: `herdr_engineering/control_plane/collab_mcp.py`
- Modify: `herdr_engineering/control_plane/collab.py`
- Create: `herdr_engineering/control_plane/skill_transfer.py`
- Test: `tests/control_plane/test_collab_mcp.py`
- Test: `tests/control_plane/test_skill_transfer.py`

**Interfaces:**
- MCP `skills_search(query, scope)`
- MCP `skills_get(skill_id, version)`
- MCP `skills_download(skill_id, version)`
- MCP `skills_install(skill_id, version)`
- `download_skill(principal, skill_id, version) -> SignedPackageRef`

- [ ] **Step 1: Add failing MCP tests** for scope filtering, authorization, package checksum, and no credential leakage.
- [ ] **Step 2: Run focused tests and verify failure.**
- [ ] **Step 3: Implement short-lived package download references and local-install audit records.**
- [ ] **Step 4: Add harness-side cache/install contract with content-addressed local paths and atomic replacement.**
- [ ] **Step 5: Run MCP security tests and commit** `git commit -m "feat: let agents install authorized collaboration skills"`.

### Task 4: Add shared skill publication review and parity UI

**Files:**
- Modify: `packages/collab-view.js`
- Modify: `packages/collab.css`
- Modify: `packages/herdr-web-client/collab.js`
- Test: `packages/herdr-web-client/skills.test.js`
- Test: `tests/playwright/collaboration-skills.test.mjs`

**Interfaces:**
- `CollabApi.skills(scope)`
- `CollabApi.publishSkill(draftId, scope)`
- `renderSkillLibrary(model)`
- `renderSkillReview(skillVersion)`

- [ ] **Step 1: Add failing tests** for private/room/shared visibility, owner/maintainer publication, version display, download, and dashboard/public parity.
- [ ] **Step 2: Run focused tests and verify failure.**
- [ ] **Step 3: Implement skill library/review views using the shared collaboration client.**
- [ ] **Step 4: Add publication confirmation showing capabilities, files, dependencies, and provenance.**
- [ ] **Step 5: Run `npm test` and Playwright skill tests.**
- [ ] **Step 6: Commit** `git commit -m "feat: add collaboration skill library UI"`.
