import json
from pathlib import Path

import pytest

from herdr_engineering.control_plane.handoff_lineage import row_from_node
from herdr_engineering.control_plane.handoffs import HandoffRegistry, validate_memory_context
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_handoff import call, server

FIX = Path(__file__).parent / "fixtures" / "handoff_pre_memory.json"
SCHEMA = Path(__file__).parents[2] / "schemas" / "handoff-memory-context-v1.schema.json"
BLOCK = {"version": 1, "refs": [{"memory_id": "mem_1", "bank_id": "bank_a", "revision": "r3"}],
         "snapshot_digest": "sha256:" + "a" * 64,
         "checkpoint": {"state": "pending", "outbox_pending": 3, "at": "2026-11-02T10:00:00Z"}}


def registry():
    return HandoffRegistry(ControlPlaneRepo())


def legacy():
    return json.loads(FIX.read_text())


def test_old_package_accepted_unchanged_without_memory_field():
    row, replayed = registry().create(legacy(), {"id": "op"})
    assert not replayed and row["state"] == "queued"
    assert "memory_context" not in row and row["goal"] == "Continue work"
    assert "some_unknown_field" not in row  # unknown fields tolerated (ignored) exactly as before


def test_block_round_trips_via_registry_page_and_restore():
    reg = registry()
    row, _ = reg.create({**legacy(), "memory_context": BLOCK}, {"id": "op"})
    assert row["memory_context"] == BLOCK
    assert reg.get("handoff_legacy_1")["memory_context"] == BLOCK
    page = reg.page()
    assert page["handoffs"][0]["memory_context"] == BLOCK
    restored = HandoffRegistry(reg.repo)
    assert restored.get("handoff_legacy_1")["memory_context"] == BLOCK


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(version=2), lambda b: b.update(bogus=1), lambda b: b.update(refs={}),
    lambda b: b["refs"][0].update(text="leaked memory text"), lambda b: b["refs"][0].pop("revision"),
    lambda b: b.update(snapshot_digest="sha1:x"), lambda b: b["checkpoint"].update(state="unknown"),
    lambda b: b["checkpoint"].update(outbox_pending=-2), lambda b: b["checkpoint"].update(at="soon"),
    lambda b: b.update(refs=[BLOCK["refs"][0]] * 101),
])
def test_malformed_rejected_and_leaves_registry_and_valid_replay_intact(mutate):
    reg = registry()
    bad = json.loads(json.dumps(BLOCK))
    mutate(bad)
    with pytest.raises(ValueError):
        reg.create({**legacy(), "memory_context": bad}, {"id": "op"})
    assert reg.get("handoff_legacy_1") is None
    row, _ = reg.create(legacy(), {"id": "op"})  # same package without the block still works
    assert row["goal"] == "Continue work" and row["remaining"] == ["test"]


def test_integer_revision_from_herdr_memory_cli_accepted_but_bool_and_negative_rejected():
    ok = json.loads(json.dumps(BLOCK))
    ok["refs"][0]["revision"] = 3
    assert validate_memory_context(ok)["refs"][0]["revision"] == 3
    for bad in (True, -1, 1.5, None):
        ok["refs"][0]["revision"] = bad
        with pytest.raises(ValueError):
            validate_memory_context(ok)


def test_null_memory_context_treated_as_absent():
    row, _ = registry().create({**legacy(), "memory_context": None}, {"id": "op"})
    assert "memory_context" not in row


def test_http_api_passthrough_and_400_on_malformed():
    instance, _ = server()
    try:
        status, created = call(instance.port, "POST", "/api/v1/handoffs", {**legacy(), "memory_context": BLOCK})
        assert status == 201 and created["memory_context"] == BLOCK
        status, got = call(instance.port, "GET", "/api/v1/handoffs/handoff_legacy_1")
        assert status == 200 and got["memory_context"] == BLOCK
        status, err = call(instance.port, "POST", "/api/v1/handoffs",
                           {**legacy(), "handoff_id": "handoff_bad", "memory_context": {"version": 1}})
        assert status == 400 and "memory_context" in err["error"]
        status, old = call(instance.port, "POST", "/api/v1/handoffs", {**legacy(), "handoff_id": "handoff_old"})
        assert status == 201 and "memory_context" not in old
    finally:
        instance.shutdown() if hasattr(instance, "shutdown") else None


def test_no_memory_text_survives_in_row():
    reg = registry()
    blob = json.dumps(reg.create({**legacy(), "memory_context": BLOCK}, {"id": "op"})[0])
    assert "leaked" not in blob


def test_lineage_row_passthrough_and_malformed_dropped():
    node = {"node_type": "handoff", "id": "handoff:h1", "state": "committed", "attrs": {"cause": "context_pressure", "memory_context": BLOCK}}
    assert row_from_node(node)["memory_context"] == BLOCK
    node["attrs"]["memory_context"] = {"version": 7}
    row = row_from_node(node)
    assert "memory_context" not in row and row["state"] == "completed"
    node["attrs"].pop("memory_context")
    assert "memory_context" not in row_from_node(node)


def test_schema_file_matches_validator_contract():
    schema = json.loads(SCHEMA.read_text())
    assert set(schema["required"]) == set(BLOCK) and schema["additionalProperties"] is False
    assert set(schema["properties"]["checkpoint"]["required"]) == set(BLOCK["checkpoint"])
    assert validate_memory_context(BLOCK) == BLOCK
