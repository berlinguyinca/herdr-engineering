"""Conformance checks for published contract fixtures; runtime validation is main-owned."""
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_event_intent_contract_is_closed_and_excludes_authority():
    schema = json.loads((ROOT / "docs/contracts/event-intent-v1.schema.json").read_text())
    assert schema["$id"] == "urn:herdr:event-intent:v1"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert not {"user_id", "task_id", "scope", "grants", "actions", "model", "guests"}.intersection(schema["required"])
    assert schema["properties"]["evidence"]["additionalProperties"] is False


def test_positive_examples_preserve_unknowns_and_ground_every_field():
    data = json.loads((ROOT / "tests/fixtures/event_coordinator/event-intent-v1.json").read_text())
    schema = json.loads((ROOT / "docs/contracts/event-intent-v1.schema.json").read_text())
    fields = set(schema["properties"]["evidence"]["required"])
    assert len(data["positive"]) >= 6
    for case in data["positive"]:
        value = case["result"]
        assert set(value) == set(schema["required"])
        assert set(value["evidence"]) == fields
        for key in fields:
            quote = value["evidence"][key]
            if value[key] is None:
                assert quote is None
            else:
                assert isinstance(quote, str) and quote and quote in case["request"]
        if value["adults"] is not None and value["children"] is not None:
            assert value["adults"] + value["children"] <= 100
        ages = value["childrenAgeRange"]
        if ages is not None:
            assert 0 <= ages["minimum"] <= ages["maximum"] <= 17
        if value["separateBills"] is not True:
            assert value["separateBillPayers"] is None
    screenshot = next(c for c in data["positive"] if c["id"] == "screenshot-age-range")
    assert screenshot["result"]["childrenAgeRange"] == {"minimum": 6, "maximum": 14}
    assert screenshot["result"]["currency"] is None


def test_negative_examples_cover_model_authority_and_grounding_errors():
    data = json.loads((ROOT / "tests/fixtures/event_coordinator/event-intent-v1.json").read_text())
    ids = {c["id"] for c in data["negative"]}
    assert {"invented-grant", "ungrounded-quote", "non-null-without-evidence", "combined-count-overflow", "reversed-age-range", "unknown-billing-payers"}.issubset(ids)
    assert all(c["validation_layer"] in {"schema", "semantic"} for c in data["negative"])
