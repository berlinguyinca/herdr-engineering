"""The harness itself: a real process, configured only through YAML, with every optional feature live."""
from tests.e2e import harness


def test_real_process_serves_generated_keys_and_all_features_answer(planes):
    plane = planes(keys={"pi": "ingest"}, users=["alice"])
    assert plane.alive() and plane.proc.pid > 0
    assert plane.get("/api/v1/health", key="viewer").status == 200
    plane.verify_features("alice")
    assert harness.principal_id(plane.keys["pi"]).startswith("key:")


def test_feature_probe_has_teeth_a_disabled_feature_fails_the_positive_control(planes):
    import pytest
    plane = planes(users=["alice"], collab={"steering": {"enabled": False}, "contextual": {"enabled": False}})
    with pytest.raises(AssertionError, match="feature off or broken"):
        plane.verify_features("alice")
