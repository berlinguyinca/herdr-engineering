"""Host state keys (versions/pins/updates/fleet_update) are not redacted by key name."""
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.redact import redact_ingest_body
from herdr_engineering.control_plane.repo import ControlPlaneRepo

EXT = {"auth-helper": "1.2.0", "token-tools": "0.3.0"}


def _host_body(**payload):
    return {"source": "fabric-agent", "entity_type": "host", "entity_id": "beast",
            "event_type": "host.heartbeat", "payload": payload}


def test_extension_names_with_secret_hints_survive():
    body = _host_body(versions={"pi": "1.0", "pi_extensions": dict(EXT)},
                      pins={"pi_extensions": dict(EXT)},
                      updates={"packages": [{"name": "libauth", "from": "1", "to": "2"}]})
    out = redact_ingest_body(body)["payload"]
    assert out["versions"]["pi_extensions"] == EXT
    assert out["pins"]["pi_extensions"] == EXT
    assert out["updates"]["packages"][0]["name"] == "libauth"


def test_secret_values_inside_state_keys_are_still_redacted():
    body = _host_body(versions={"pi": "sk-abcdefghijklmnop", "pi_extensions": {"x": "Bearer abc.def"}})
    out = redact_ingest_body(body)["payload"]["versions"]
    assert "sk-abcdefghijklmnop" not in str(out)
    assert "abc.def" not in str(out)


def test_other_host_keys_and_other_entities_keep_key_redaction():
    body = _host_body(token="t0ps3cret-value", versions={"pi": "1"})
    assert redact_ingest_body(body)["payload"]["token"] == "[REDACTED]"
    other = {"entity_type": "session", "entity_id": "s", "event_type": "session.started",
             "payload": {"versions": {"auth-helper": "1"}}}
    assert redact_ingest_body(other)["payload"]["versions"]["auth-helper"] == "[REDACTED]"


def test_no_spurious_version_drift_for_secret_looking_extension_names():
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    svc.ingest(redact_ingest_body(_host_body(
        host_name="beast", telemetry={"cpu": 1.0},
        versions={"pi_extensions": dict(EXT)})))
    svc.ingest(redact_ingest_body({
        "source": "fleet-update", "entity_type": "host", "entity_id": "beast",
        "event_type": "host.fleet_update", "payload": {"pins": {"pi_extensions": dict(EXT)}}}))
    codes = {p["code"] for p in repo.get_host("beast")["problems"]}
    assert "version_drift" not in codes
