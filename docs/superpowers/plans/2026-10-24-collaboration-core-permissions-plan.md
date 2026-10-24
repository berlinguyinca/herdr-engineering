# Collaboration Core and Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make user approval, room membership, agent delegation, file permission, room removal, and irreversible user deletion explicit and independently testable.

**Architecture:** Extend `CollabService` and its event projection with separate records and transitions instead of overloading `kicked`. Preserve the existing append-only event compatibility while adding the durable Postgres path required by the data-platform plan.

**Tech Stack:** Python, existing `herdr_engineering.control_plane.collab`, Cognito identity adapter, existing REST/MCP/A2A routes, pytest.

**Spec:** `docs/superpowers/specs/2026-10-24-collaboration-rooms-data-platform-design.md`

## Global Constraints

- “Remove from room” is room-scoped and reversible; “Delete person permanently” is global and audited.
- Deleted users cannot be revived by old sessions, tokens, invitations, join tokens, or agent credentials.
- Room-owned file bytes survive user deletion; user attribution and authored content do not.
- Approval, room membership, agent access, and file-upload permission are separate capabilities.

### Task 1: Add explicit lifecycle records and event names

**Files:**
- Modify: `herdr_engineering/control_plane/collab.py`
- Modify: `herdr_engineering/control_plane/ids.py`
- Test: `tests/control_plane/test_collab.py`
- Test: `tests/control_plane/test_collab_approval.py`

**Interfaces:**
- Produce `user_status`, `room_membership_status`, `agent_permission_status`, and `file_upload_permission` in all relevant room/people responses.
- Produce events named `user_approved`, `room_member_invited`, `room_member_approved`, `room_member_removed`, `agent_access_requested`, `agent_access_approved`, `agent_access_revoked`, and `file_upload_permission_changed`.

- [ ] **Step 1: Write failing transition tests** for room-specific removal, persistent agent permission, and per-member upload permission.

```python
def test_remove_room_member_does_not_delete_global_user_or_other_memberships(service, alice, bob):
    room_a = service.create_room(alice, {"name": "a"})["room_id"]
    room_b = service.create_room(alice, {"name": "b"})["room_id"]
    service.invite_member(alice, room_a, {"user_id": bob.id})
    service.invite_member(alice, room_b, {"user_id": bob.id})
    service.decide_room_member(alice, room_a, bob.id, "approve")
    service.decide_room_member(alice, room_b, bob.id, "approve")
    service.remove_room_member(alice, room_a, bob.id)
    assert service.user_status(bob.id) == "approved"
    assert service.room_member(room_b, bob.id)["status"] == "approved"
    assert service.room_member(room_a, bob.id)["status"] == "removed"
```

- [ ] **Step 2: Run the focused tests and verify they fail** with the existing overloaded status/action behavior.

Run: `pytest -q tests/control_plane/test_collab.py::test_remove_room_member_does_not_delete_global_user_or_other_memberships`

Expected: FAIL because the room-specific transition and response fields do not exist.

- [ ] **Step 3: Implement the smallest event/state projection** with separate dictionaries/records and idempotency keys for each transition.
- [ ] **Step 4: Run the focused approval and collaboration tests.**

Run: `pytest -q tests/control_plane/test_collab.py tests/control_plane/test_collab_approval.py`

Expected: PASS with existing unrelated tests unchanged.

- [ ] **Step 5: Commit** `git add herdr_engineering/control_plane/collab.py herdr_engineering/control_plane/ids.py tests/control_plane/test_collab.py tests/control_plane/test_collab_approval.py && git commit -m "feat: separate collaboration lifecycle permissions"`

### Task 2: Implement global deletion and archive-aware scrubbing hooks

**Files:**
- Modify: `herdr_engineering/control_plane/collab.py`
- Modify: `herdr_engineering/control_plane/collab_cognito.py`
- Test: `tests/control_plane/test_collab_pentest.py`
- Test: `tests/control_plane/test_collab_hardening.py`

**Interfaces:**
- Add `delete_person(principal, user_id, reason) -> dict` returning only deletion status and audit ID.
- Add a storage callback `scrub_deleted_user(user_id) -> ScrubReport` for the storage track.
- All credential validation paths must reject a deleted user before room lookup.

- [ ] **Step 1: Add failing tests** proving old bearer tokens, agent credentials, invitations, join tokens, and active streams stop working after deletion.
- [ ] **Step 2: Run the tests and verify failure** with `pytest -q tests/control_plane/test_collab_pentest.py -k deleted`.
- [ ] **Step 3: Implement delete ordering:** mark deleted, revoke credentials/invitations/memberships, scrub authored content through the storage callback, then emit only minimal audit metadata.
- [ ] **Step 4: Add tests** proving room files remain and uploader attribution is absent after deletion.
- [ ] **Step 5: Run `pytest -q tests/control_plane/test_collab_pentest.py tests/control_plane/test_collab_hardening.py`.**
- [ ] **Step 6: Commit** `git commit -am "feat: add audited global collaboration deletion"`.

### Task 3: Add agent access requests and ongoing grants

**Files:**
- Modify: `herdr_engineering/control_plane/collab.py`
- Modify: `herdr_engineering/control_plane/collab_mcp.py`
- Modify: `herdr_engineering/control_plane/collab_a2a.py`
- Test: `tests/control_plane/test_collab_tasks.py`
- Test: `tests/control_plane/test_collab_a2a.py`

**Interfaces:**
- `request_agent_access(principal, room_id, agent_id) -> dict`
- `decide_agent_access(principal, request_id, decision) -> dict`
- `list_agent_access_requests(principal, room_id) -> dict`
- MCP task submission returns a structured `access_request` response when no grant exists.

- [ ] **Step 1: Write failing tests** for conversational request response, owner approval, ongoing reuse, and revocation.
- [ ] **Step 2: Run `pytest -q tests/control_plane/test_collab_tasks.py -k access` and verify failure.**
- [ ] **Step 3: Implement request/grant projection and owner-only decision authorization.**
- [ ] **Step 4: Add MCP/A2A response envelopes without exposing credentials or private room data.**
- [ ] **Step 5: Run `pytest -q tests/control_plane/test_collab_tasks.py tests/control_plane/test_collab_a2a.py`.**
- [ ] **Step 6: Commit** `git commit -am "feat: add ongoing per-agent room access requests"`.

### Task 4: Add public waiting-room state and coverage

**Files:**
- Modify: `herdr_engineering/control_plane/collab.py`
- Modify: `packages/herdr-web-client/collab.js`
- Modify: `packages/collab-view.js`
- Modify: `packages/collab.css`
- Test: `tests/control_plane/test_collab_public.py`
- Test: `packages/herdr-web-client/collab.test.js`

**Interfaces:**
- `GET /api/v1/collab/me` returns `pending`, `approved`, `denied`, or `deleted` without room enumeration.
- `CollabApi.me()` returns the status envelope used by both standalone and dashboard views.

- [ ] **Step 1: Add failing API/client/UI tests** for the nonblank waiting page and status transition.
- [ ] **Step 2: Run the focused Python and Node tests and verify failure.**
- [ ] **Step 3: Implement the shared waiting-state renderer and polling/SSE lifecycle.**
- [ ] **Step 4: Run `pytest -q tests/control_plane/test_collab_public.py` and `npm test -- --test-name-pattern='waiting|approval'`.**
- [ ] **Step 5: Commit** `git commit -am "feat: expose collaboration waiting room"`.
