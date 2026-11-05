"""Accessor never renders a full event/query or exports excluded body fields."""


import pytest

from herdr_engineering.control_plane.inspector import InspectorStore


def test_metadata_accessor_ignores_prose_diffs_commands_names_and_labels():
    store = InspectorStore()
    store.ingest(
        "session",
        {
            "events": [
                {
                    "kind": "file",
                    "path": "src/a.py",
                    "op": "edit",
                    "diff": "+PROSE_A",
                    "agent_id": "not-a-grant",
                },
                {
                    "kind": "test",
                    "run_id": "run-a",
                    "phase": "end",
                    "passed": 4,
                    "failed": 0,
                    "skipped": 1,
                    "command": "PROSE_COMMAND",
                    "failures": [{"name": "PROSE_TEST", "message": "PROSE_FAILURE"}],
                },
                {"kind": "message", "text": "PERSONAL_MCP_PROSE", "role": "assistant"},
                {"kind": "tool", "call_id": "mcp", "phase": "end", "output": "TOOL_PROSE"},
            ]
        },
    )
    rows = store.activity_metadata("session", role="operator")
    assert len(rows) == 2 and {row.kind for row in rows} == {"file", "test"}
    assert "PROSE" not in repr(rows) and "not-a-grant" not in repr(rows)
    with store._lock:
        for entry in store._sessions["session"].events:
            if entry["kind"] in ("file", "test"):
                entry["data"]["diff"] = "OTHER_DIFF"
                entry["data"]["command"] = "OTHER_COMMAND"
                entry["data"]["failures"] = [{"name": "OTHER_TEST", "message": "OTHER_MESSAGE"}]
                entry["redacted"] = True
                entry["truncated"] = True
                entry["agent_id"] = "engineering"
    assert store.activity_metadata("session", role="operator") == rows


def test_accessor_default_role_cannot_declassify_and_does_not_create_sessions():
    store = InspectorStore()
    with pytest.raises(PermissionError):
        store.activity_metadata("missing")
    with pytest.raises(KeyError):
        store.activity_metadata("missing", role="operator")
    assert not store._sessions


def test_metadata_revision_is_available_but_completed_counts_stay_uncoerced():
    store = InspectorStore()
    store.ingest("session", {"kind": "test", "run_id": "run", "phase": "start"})
    first = store.activity_metadata("session", role="operator")[0]
    store.ingest(
        "session",
        {"kind": "test", "run_id": "run", "phase": "end", "passed": 1.5, "failed": 0, "skipped": 0},
    )
    last = store.activity_metadata("session", role="operator")[0]
    assert last.sequence == first.sequence and last.revision > first.revision
    assert last.passed == 1.5 and type(last.passed) is float
