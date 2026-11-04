import copy

import pytest

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def test_generic_ingest_cannot_append_operator_host_policy_receipts():
    repo = ControlPlaneRepo()
    with pytest.raises(ValueError, match="reserved for the server"):
        IngestService(repo).ingest({"entity_type": "host", "entity_id": "mac",
                                  "event_type": "HostPolicyChanged",
                                  "payload": {"mode": "automatic", "revision": 99}})
    assert repo.stream("host", "mac") == []


def test_generic_host_heartbeat_cannot_grant_scheduler_authority():
    repo = ControlPlaneRepo()
    repo.upsert_host("mac", "mac", None, host_uid="trusted-mac",
                     scheduler_owned=True, scheduler_trusted=True,
                     scheduler_policy={"mode": "manual_only", "revision": 1},
                     scheduler_capacity={"total_cpu": 0})
    IngestService(repo).ingest({"entity_type": "host", "entity_id": "mac",
                              "event_type": "host.heartbeat", "payload": {
                                  "host_uid": "forged-uid", "scheduler_owned": False,
                                  "scheduler_trusted": False,
                                  "scheduler_policy": {"mode": "automatic", "revision": 99},
                                  "scheduler_capacity": {"total_cpu": 100}}})
    host = repo.get_host("mac")
    assert host["host_uid"] == "trusted-mac"
    assert host["scheduler_policy"] == {"mode": "manual_only", "revision": 1}
    assert host["scheduler_capacity"] == {"total_cpu": 0}
    assert host["scheduler_owned"] and host["scheduler_trusted"]


@pytest.mark.parametrize("envelope", [None, {}, {"content_sha256": "old-client"}])
def test_historical_client_envelope_policy_is_ignored_during_replay(envelope):
    from herdr_engineering.control_plane.bridge import _reconstruct_from_events

    repo = ControlPlaneRepo()
    repo.upsert_host("mac", "mac", None, scheduler_policy={"mode": "manual_only", "revision": 1})
    repo.set_host_policy("mac", mode="disabled", expected_revision=1)
    repo.record_event("host", "mac", "HostPolicyChanged", {
        "mode": "automatic", "revision": 99, "_envelope": envelope})
    state = repo.export_state()
    restored = ControlPlaneRepo()
    restored.import_state({"version": 1, "events": state["events"], "event_order": state["event_order"]})
    assert restored.get_host("mac")["scheduler_policy"]["mode"] == "disabled"
    _, _, hosts, _, _ = _reconstruct_from_events(state["events"])
    assert hosts["mac"]["scheduler_policy"]["mode"] == "disabled"
    contaminated = copy.deepcopy(state)
    contaminated["hosts"]["mac"]["scheduler_policy"] = {
        "mode": "automatic", "revision": 99, "_envelope": envelope}
    restored.import_state(contaminated)
    assert restored.get_host("mac")["scheduler_policy"]["mode"] == "disabled"
    contaminated["events"] = {}
    contaminated["event_order"] = []
    restored.import_state(contaminated)
    assert restored.get_host("mac")["scheduler_policy"]["mode"] == "disabled"
