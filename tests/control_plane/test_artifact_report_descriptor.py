import copy
import json
from pathlib import Path

import pytest

from herdr_engineering.control_plane.artifact_report_descriptor import (
    MAX_DESCRIPTOR_BYTES,
    ReportDescriptorError,
    report_diff,
    validate_report_descriptor,
)

FIXTURES = Path(__file__).parent / "fixtures" / "reports"


def descriptor(name="benchmark"):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def changed_categories(diff):
    return {name for name, changes in diff["categories"].items() if changes["changed"]}


def test_quantitative_and_qualitative_descriptors_need_no_domain_specific_fields():
    for name in ("benchmark", "incident-audit"):
        original = descriptor(name)
        result = validate_report_descriptor(original)
        assert result == original
        assert result is not original
        result["findings"][0]["statement"] = "edited copy"
        assert original["findings"][0]["statement"] != "edited copy"


def test_finding_references_must_exist_in_the_correct_collection():
    for field, identifier in (("question_ids", "missing-question"),
                              ("evidence_ids", "figure-1"),
                              ("figure_ids", "missing-figure")):
        value = descriptor()
        value["findings"][0][field] = [identifier]
        with pytest.raises(ReportDescriptorError, match=field):
            validate_report_descriptor(value)
    value = descriptor()
    value["findings"][0]["evidence_ids"] = []
    with pytest.raises(ReportDescriptorError, match="evidence_ids"):
        validate_report_descriptor(value)


def test_evidence_and_figure_asset_links_must_resolve():
    value = descriptor()
    value["evidence"][0]["asset_ids"] = ["missing"]
    with pytest.raises(ReportDescriptorError, match="asset_ids"):
        validate_report_descriptor(value)
    value = descriptor()
    value["figures"][0]["asset_id"] = "missing"
    with pytest.raises(ReportDescriptorError, match="asset_id"):
        validate_report_descriptor(value)


@pytest.mark.parametrize("collection", ["questions", "evidence", "findings", "figures", "assets"])
def test_duplicate_stable_ids_are_rejected(collection):
    value = descriptor()
    value[collection].append(copy.deepcopy(value[collection][0]))
    with pytest.raises(ReportDescriptorError, match="duplicate"):
        validate_report_descriptor(value)


@pytest.mark.parametrize("name", ["/tmp/private.csv", "../data.csv", "data/../file.csv",
                                  "./file.csv", "C:/data.csv", "data\\file.csv",
                                  "https://example.org/data.csv", "data//file.csv", "bad\x00.csv"])
def test_asset_names_cannot_escape_or_be_arbitrary_urls(name):
    value = descriptor()
    value["assets"][0]["name"] = name
    with pytest.raises(ReportDescriptorError, match="name"):
        validate_report_descriptor(value)


def test_duplicate_asset_names_and_invalid_metadata_are_rejected():
    value = descriptor()
    value["assets"][1]["name"] = value["assets"][0]["name"]
    with pytest.raises(ReportDescriptorError, match="duplicate"):
        validate_report_descriptor(value)
    for key, invalid in (("size_bytes", -1), ("size_bytes", True), ("size_bytes", 1.5),
                         ("sha256", "unknown"), ("media_type", "text/html\r\nX: bad"),
                         ("role", "precursor")):
        value = descriptor()
        value["assets"][0][key] = invalid
        with pytest.raises(ReportDescriptorError, match=key):
            validate_report_descriptor(value)


def test_unknown_fields_and_non_json_values_fail_without_mutating_input():
    value = descriptor()
    value["findingz"] = []
    with pytest.raises(ReportDescriptorError, match="findingz"):
        validate_report_descriptor(value)
    for invalid in (float("nan"), float("inf"), object()):
        value = descriptor()
        value["analysis"]["configuration"]["invalid"] = invalid
        with pytest.raises(ReportDescriptorError):
            validate_report_descriptor(value)
    value = descriptor()
    value["schema_version"] = True
    with pytest.raises(ReportDescriptorError, match="schema_version"):
        validate_report_descriptor(value)


def test_descriptor_is_bounded_even_with_many_short_configuration_values():
    value = descriptor()
    value["analysis"]["configuration"] = {"rows": ["x" * 100] * (MAX_DESCRIPTOR_BYTES // 100)}
    with pytest.raises(ReportDescriptorError, match="size|bytes"):
        validate_report_descriptor(value)


def test_nested_configuration_is_bounded_and_cannot_create_recursion_errors():
    value = descriptor()
    nested = {}
    value["analysis"]["configuration"] = nested
    for _ in range(50):
        nested["nested"] = {}
        nested = nested["nested"]
    with pytest.raises(ReportDescriptorError, match="depth"):
        validate_report_descriptor(value)


def test_output_manifest_checks_name_digest_and_available_metadata():
    value = descriptor()
    outputs = [dict(asset, storage_key=f"sha256/{asset['sha256']}") for asset in value["assets"]]
    assert validate_report_descriptor(value, outputs)
    for field, invalid in (("name", "other.csv"), ("sha256", "f" * 64),
                           ("size_bytes", 999999), ("media_type", "application/octet-stream")):
        changed = copy.deepcopy(outputs)
        changed[0][field] = invalid
        with pytest.raises(ReportDescriptorError, match="manifest"):
            validate_report_descriptor(value, changed)


def test_output_manifest_does_not_treat_boolean_size_as_an_integer():
    value = descriptor()
    value["assets"][0]["size_bytes"] = 1
    manifest = copy.deepcopy(value["assets"])
    manifest[0]["size_bytes"] = True
    with pytest.raises(ReportDescriptorError, match="manifest"):
        validate_report_descriptor(value, manifest)


def test_reordering_id_collections_does_not_change_semantic_content():
    left = descriptor()
    left["questions"].append({"id": "question-2", "text": "Is uncertainty known?"})
    right = copy.deepcopy(left)
    right["questions"].reverse()
    right["assets"].reverse()
    assert not report_diff(left, right)["changed"]
    assert [item["id"] for item in validate_report_descriptor(right)["questions"]] == ["question-1", "question-2"]


def test_editorial_revision_keeps_data_and_analysis_unchanged():
    left = descriptor()
    right = copy.deepcopy(left)
    right["findings"][0]["statement"] = "Clarified wording of the same observed comparison."
    right["summary"] = "A clearer introduction."
    diff = report_diff(left, right)
    assert changed_categories(diff) == {"editorial"}
    assert any("findings/finding-1/statement" in item["path"]
               for item in diff["categories"]["editorial"]["modified"])


def test_data_analysis_and_presentation_changes_have_separate_categories():
    left = descriptor()
    cases = [("data", lambda value: value["assets"][0].update(sha256="e" * 64)),
             ("analysis", lambda value: value["analysis"]["configuration"].update(warmup=8)),
             ("presentation", lambda value: value["figures"][0].update(caption="Reworded figure caption"))]
    for expected, mutate in cases:
        right = copy.deepcopy(left)
        mutate(right)
        assert changed_categories(report_diff(left, right)) == {expected}


def test_evidence_labels_are_editorial_but_observations_are_data():
    left = descriptor("incident-audit")
    right = copy.deepcopy(left)
    right["evidence"][0]["title"] = "A clearer source label"
    assert changed_categories(report_diff(left, right)) == {"editorial"}
    right = copy.deepcopy(left)
    right["evidence"][0]["description"] = "A newly supplied observation changes the evidence."
    assert changed_categories(report_diff(left, right)) == {"data"}


def test_source_hash_changes_are_analysis_and_nested_array_order_is_meaningful():
    left = descriptor()
    right = copy.deepcopy(left)
    right["analysis"]["source_hashes"]["benchmark.py"] = "f" * 64
    assert changed_categories(report_diff(left, right)) == {"analysis"}
    right = copy.deepcopy(left)
    right["analysis"]["configuration"]["trial_order"].reverse()
    assert changed_categories(report_diff(left, right)) == {"analysis"}


def test_added_and_removed_ids_and_long_changes_are_explicit_and_bounded():
    left = descriptor()
    right = copy.deepcopy(left)
    right["questions"].append({"id": "question-2", "text": "Unanswered follow-up"})
    diff = report_diff(left, right)
    assert any(item["path"] == "/questions/question-2" for item in diff["categories"]["editorial"]["added"])
    inverse = report_diff(right, left)
    assert any(item["path"] == "/questions/question-2" for item in inverse["categories"]["editorial"]["removed"])
    right["findings"][0]["statement"] = "A" * 12000
    diff = report_diff(left, right)
    modified = next(item for item in diff["categories"]["editorial"]["modified"]
                    if item["path"].endswith("/statement"))
    assert modified["new"]["truncated"] is True
    assert len(json.dumps(diff)) < 10000


def test_diff_caps_details_without_hiding_total_changes():
    left = descriptor()
    left["questions"] = [{"id": f"question-{index}", "text": "Original question"}
                         for index in range(300)]
    right = copy.deepcopy(left)
    for question in right["questions"]:
        question["text"] = "Changed question"
    diff = report_diff(left, right)
    assert diff["changed"] and diff["truncated"]
    assert diff["change_count"] == 300
    assert diff["categories"]["editorial"]["counts"]["modified"] == 300
    assert len(diff["categories"]["editorial"]["modified"]) == diff["detail_limit"]


def test_schema_and_validator_both_reject_newline_in_identifiers_and_mime():
    jsonschema = pytest.importorskip("jsonschema", reason="optional external schema conformance")

    schema = json.loads((Path(__file__).parents[2] / "schemas" / "artifact-report-v1.schema.json").read_text())
    for location, key in (("questions", "id"), ("assets", "media_type")):
        value = descriptor()
        value[location][0][key] += "\n"
        with pytest.raises(ReportDescriptorError):
            validate_report_descriptor(value)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(value, schema)


def test_checked_in_schema_accepts_both_fixtures_and_rejects_typos():
    jsonschema = pytest.importorskip("jsonschema", reason="optional external schema conformance")

    schema_path = Path(__file__).parents[2] / "schemas" / "artifact-report-v1.schema.json"
    schema = json.loads(schema_path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    for name in ("benchmark", "incident-audit"):
        jsonschema.validate(descriptor(name), schema)
    bad = descriptor()
    bad["findings"][0]["evidnce_ids"] = ["evidence-1"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
