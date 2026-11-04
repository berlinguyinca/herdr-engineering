"""Repeated references and cycles must never return unsanitized source objects."""
import copy
import json

from herdr_engineering.control_plane.redact import redact_payload


def test_shared_containers_and_repeated_strings_are_redacted_without_source_mutation():
    shared = {"api_key": "private-execution-proof"}
    bearer = "Bearer private-shared-token-proof"
    payload = {"first": shared, "second": shared, "text": [bearer, bearer]}
    baseline = copy.deepcopy(payload)
    result = redact_payload(payload)
    assert result["first"]["api_key"] == result["second"]["api_key"] == "[REDACTED]"
    assert result["text"] == ["[REDACTED]", "[REDACTED]"]
    assert "private-execution-proof" not in json.dumps(result)
    assert result["first"] is result["second"] and result["first"] is not shared
    assert payload == baseline


def test_cycles_are_masked_and_shared_dags_are_memoized():
    cyclic = {"api_key": "private"}
    cyclic["cycle"] = cyclic
    result = redact_payload(cyclic)
    assert result == {"api_key": "[REDACTED]", "cycle": "[REDACTED]"}
    shared = {"token": "private"}
    for _ in range(40):
        shared = [shared, shared]
    result = redact_payload(shared)
    for _ in range(40):
        assert result[0] is result[1]
        result = result[0]
    assert result == {"token": "[REDACTED]"}
