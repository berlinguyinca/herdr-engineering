"""Metadata observation cannot mutate fabric routing or service lifecycle."""
import copy
import io
import json
from types import SimpleNamespace

import pytest

import herdr_engineering.fabric_agent as fabric
import herdr_engineering.service_metadata_feed as feed_module
from herdr_engineering.service_metadata_feed import MetadataClient, ServiceMetadataFeed


class Client:
    def __init__(self, services):
        self.services = services
        self.posts = []
        self.accept = True

    def request(self, route, body=None):
        if route.startswith("/api/v1/services"):
            assert body is None
            self.service_routes = getattr(self, "service_routes", []) + [route]
            return {"services": copy.deepcopy(self.services)}
        assert route == "/api/v1/ingest"
        self.posts.append(copy.deepcopy(body))
        return {"ok": self.accept, "entity": {"entity_id": body["entity_id"]}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    state = tmp_path / "fabric-agent.json"
    state.write_text(json.dumps({"9001": {"label": "python on host (port 9001)",
        "source_port": 9001, "external_port": 19001, "lease_id": "lease-1"}}))
    service = {"service_id": "service-1", "host_id": "host",
               "service_name": "python on host (port 9001)", "source_port": 9001}
    client = Client([service])
    ticks = [0.0]
    feed = ServiceMetadataFeed(client, host_id="host", state_file=state, clock=lambda: ticks[0])
    proof = {"kind": "herdr_process_ancestry", "host_id": "host", "session_id": "herdr:host:eng:p1",
             "listener_pid": 101, "listener_start": "boot:123", "observed_at": "2026-10-20T00:00:00+00:00",
             "herdr_session": "eng", "pane_id": "p1", "terminal_id": "term_1",
             "shell_pid": 100, "shell_start": "boot:100", "ancestor_pids": [101, 100]}
    feed.ownership = SimpleNamespace(classify=lambda *args, **kwargs: dict(proof))
    feed.git = SimpleNamespace(read=lambda *args, **kwargs: {"repository_id": "github.com/team/app", "commit_sha": "a" * 40})
    listeners = {9001: {"pid": 101}}
    monkeypatch.setattr(feed_module, "scan_listening_ports", lambda: listeners)
    monkeypatch.setattr(feed_module, "_process_identity", lambda pid: {"parent_pid": 1, "start": "boot:123"} if pid else None)
    monkeypatch.setattr(feed_module, "_debug_listener", lambda pid: False)
    return SimpleNamespace(feed=feed, client=client, state=state, ticks=ticks,
                           proof=proof, listeners=listeners, service=service)


def test_observation_is_existing_id_metadata_only_and_never_instantiates_forwarders(setup, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("metadata observer must never own forwarding or fabric state")
    monkeypatch.setattr(fabric.FabricAgent, "__init__", forbidden)
    monkeypatch.setattr(fabric.AgentForwarder, "start", forbidden)
    monkeypatch.setattr(fabric.FabricAgent, "_save_store", forbidden)
    before = setup.state.read_bytes(), setup.state.stat().st_mtime_ns
    summary = setup.feed.run_once()
    assert summary["owned"] == summary["linked"] == summary["posted"] == 1
    envelope = setup.client.posts[0]
    assert envelope["entity_id"] == "service-1"
    assert envelope["event_type"] == "service.metadata"
    assert envelope["payload"]["state"] == "observed"
    assert envelope["payload"]["listener_start"] == "boot:123"
    assert not set(envelope["payload"]) & {"status", "port", "route", "updated_at", "service_name", "lease_id"}
    assert before == (setup.state.read_bytes(), setup.state.stat().st_mtime_ns)


def test_dry_run_only_reads_control_plane_and_state(setup):
    summary = setup.feed.run_once(dry_run=True)
    assert summary["mapped"] == summary["owned"] == 1
    assert summary["posted"] == 0
    assert setup.client.posts == []
    assert not setup.feed.sent


def test_unregistered_owned_listener_is_visible_as_a_gap_without_service_creation(setup):
    setup.listeners[44000] = {"pid": 101}
    summary = setup.feed.run_once(dry_run=True)
    assert summary["unregistered_listeners"] == summary["unregistered_owned"] == 1
    assert summary["mapped"] == 1
    assert summary["posted"] == 0
    assert setup.client.posts == []


@pytest.mark.parametrize("field,value", [("host_id", "other"), ("service_name", "other"), ("source_port", 9002)])
def test_no_label_cwd_or_port_guessing_across_records(setup, field, value):
    setup.client.services[0][field] = value
    summary = setup.feed.run_once()
    assert summary["unmapped"] == 1
    assert summary["posted"] == 0


def test_duplicate_control_plane_matches_are_ambiguous(setup):
    setup.client.services.append({**setup.service, "service_id": "other"})
    summary = setup.feed.run_once()
    assert summary["ambiguous"] == summary["unmapped"] == 1
    assert not setup.client.posts


@pytest.mark.parametrize("listener", [None, {}, {"pid": 101, "pid_ambiguous": True}])
def test_absent_or_ambiguous_listener_is_unconfirmed_never_a_negative_origin(setup, listener):
    setup.listeners.clear()
    if listener is not None:
        setup.listeners[9001] = listener
    summary = setup.feed.run_once()
    payload = setup.client.posts[0]["payload"]
    assert summary["unconfirmed"] == 1
    assert payload["state"] == "unconfirmed"
    assert "ownership" not in payload and "repository_id" not in payload and "listener_pid" not in payload


def test_process_reuse_during_provenance_read_is_unconfirmed(setup, monkeypatch):
    identities = iter([{"start": "boot:123"}, {"start": "boot:456"}])
    monkeypatch.setattr(feed_module, "_process_identity", lambda pid: next(identities))
    assert setup.feed.run_once()["unconfirmed"] == 1
    assert "ownership" not in setup.client.posts[0]["payload"]


def test_unproven_live_instance_has_explicit_unknown_ownership(setup):
    setup.feed.ownership = SimpleNamespace(classify=lambda *args, **kwargs: None)
    setup.feed.git = SimpleNamespace(read=lambda *args, **kwargs: {})
    assert setup.feed.run_once()["observed"] == 1
    payload = setup.client.posts[0]["payload"]
    assert payload["listener_pid"] == 101
    assert payload["ownership"] is None and payload["repository_id"] is None


def test_closed_pane_keeps_verified_origin_only_for_the_same_listener_birth(setup, monkeypatch):
    service = setup.client.services[0]
    service.update(session_id=setup.proof["session_id"], ownership=dict(setup.proof),
        verified_origin={"kind": "herdr_launch_origin", "service_id": service["service_id"],
            "host_id": "host", "session_id": setup.proof["session_id"], "ownership": dict(setup.proof)})
    setup.feed.ownership = SimpleNamespace(classify=lambda *args, **kwargs: None)
    assert setup.feed.run_once()["owned"] == 1
    monkeypatch.setattr(feed_module, "_process_identity", lambda pid: {"start": "boot:replacement"})
    setup.feed.git = SimpleNamespace(read=lambda *args, **kwargs: {})
    assert setup.feed.run_once()["owned"] == 0
    assert setup.client.posts[-1]["payload"]["ownership"] is None


def test_receipt_for_another_service_never_proves_listener_origin(setup):
    setup.client.services[0].update(session_id=setup.proof["session_id"], ownership=dict(setup.proof),
        verified_origin={"kind": "herdr_launch_origin", "service_id": "other-service",
            "host_id": "host", "session_id": setup.proof["session_id"], "ownership": dict(setup.proof)})
    setup.feed.ownership = SimpleNamespace(classify=lambda *args, **kwargs: None)
    assert setup.feed.run_once()["owned"] == 0


def test_owned_observations_deduplicate_but_refresh_within_sixty_seconds(setup):
    assert setup.feed.run_once()["posted"] == 1
    setup.proof["observed_at"] = "2026-10-20T00:00:30+00:00"
    setup.ticks[0] = 30
    assert setup.feed.run_once()["unchanged"] == 1
    setup.ticks[0] = 60
    assert setup.feed.run_once()["posted"] == 1
    setup.feed.git = SimpleNamespace(read=lambda *args, **kwargs: {"repository_id": "github.com/team/app", "commit_sha": "b" * 40})
    assert setup.feed.run_once()["posted"] == 1


def test_inconsistent_helper_proof_never_publishes_owned_evidence(setup):
    setup.proof["listener_start"] = "boot:replacement"
    assert setup.feed.run_once()["unconfirmed"] == 1
    assert "ownership" not in setup.client.posts[0]["payload"]


def test_unassigned_and_missing_observations_only_publish_changes(setup):
    setup.feed.ownership = SimpleNamespace(classify=lambda *args, **kwargs: None)
    setup.feed.git = SimpleNamespace(read=lambda *args, **kwargs: {})
    assert setup.feed.run_once()["posted"] == 1
    setup.ticks[0] = 86400
    assert setup.feed.run_once()["unchanged"] == 1
    setup.listeners.clear()
    assert setup.feed.run_once()["posted"] == 1
    setup.ticks[0] *= 2
    assert setup.feed.run_once()["unchanged"] == 1


def test_rejected_update_is_not_cached_and_retries(setup):
    setup.client.accept = False
    assert setup.feed.run_once()["errors"] == 1
    assert not setup.feed.sent
    setup.client.accept = True
    assert setup.feed.run_once()["posted"] == 1


def test_failed_state_read_never_posts_missing_or_changes_prior_cache(setup):
    setup.feed.run_once()
    cached = copy.deepcopy(setup.feed.sent)
    setup.state.write_text("invalid")
    summary = setup.feed.run_once()
    assert summary["errors"] == 1
    assert summary["posted"] == 0
    assert setup.feed.sent == cached


def test_transport_refuses_gateway_routes_and_mutating_inventory_reads():
    client = MetadataClient("http://control.test")
    with pytest.raises(ValueError):
        client.request("/api/leases", {})
    with pytest.raises(ValueError):
        client.request("/api/v1/services", {})
    with pytest.raises(ValueError):
        client.request("/api/v1/ingest")
    with pytest.raises(ValueError):
        client.request("/api/v1/ingest?x=1", {})
    with pytest.raises(ValueError):
        MetadataClient("https://user:secret@control.test")


def test_real_backend_accepts_observer_payload_without_lifecycle_or_lease_mutation(setup):
    from herdr_engineering.control_plane.ingest import IngestService
    from herdr_engineering.control_plane.repo import ControlPlaneRepo

    repo = ControlPlaneRepo()
    setup.proof.update(herdr_session="eng", pane_id="p1", terminal_id="term_1",
                       shell_pid=100, shell_start="boot:100", ancestor_pids=[101, 100])
    repo.upsert_session("herdr:host:eng:p1", host_id="host", herdr_session="eng", pane_id="p1", terminal_id="term_1")
    service_id = repo.register_service(setup.service["service_name"], "host", 19001,
                                       source_port=9001, route="http://dev:19001")
    before = repo.get_service(service_id)

    class Backend:
        def request(self, route, body=None):
            if route.startswith("/api/v1/services"):
                return {"services": repo.list_services()}
            assert route == "/api/v1/ingest" and body["event_type"] == "service.metadata"
            return {"ok": True, "entity": IngestService(repo).ingest(body)}

    setup.feed.client = Backend()
    assert setup.feed.run_once()["posted"] == 1
    row = repo.get_service(service_id)
    assert row["catalog"]["scope"] == "herdr"
    assert row["repository_id"] == "github.com/team/app"
    for field in ["service_id", "status", "port", "route", "url", "source_port", "created_at", "updated_at"]:
        assert row[field] == before[field]
    assert len(repo.list_services()) == 1


def test_collection_budget_rotates_remaining_registered_entries(setup):
    setup.state.write_text(json.dumps({str(p): {"label": f"app-{p}", "source_port": p}
                                      for p in range(9001, 9004)}))
    setup.client.services = [{"service_id": f"s-{p}", "host_id": "host", "service_name": f"app-{p}", "source_port": p}
                             for p in range(9001, 9004)]
    setup.listeners.update({p: {"pid": 101} for p in range(9001, 9004)})

    def classify(*args, **kwargs):
        setup.ticks[0] += 21
        return dict(setup.proof)

    setup.feed.ownership = SimpleNamespace(classify=classify)
    first, summary = setup.feed.collect()
    second, _ = setup.feed.collect()
    assert summary["truncated"] is True
    assert first[0]["entity_id"] == "s-9001"
    assert second[0]["entity_id"] == "s-9002"


@pytest.fixture
def dynamic(setup, monkeypatch):
    setup.state.write_text("{}")
    setup.client.services = []
    setup.listeners = fabric.ListenerInventory({44000: {"pid": 101, "process": "node", "addrs": {"127.0.0.1"}}}, complete=True)
    monkeypatch.setattr(feed_module, "scan_listening_ports", lambda: setup.listeners)
    monkeypatch.setattr(feed_module, "_debug_listener", lambda pid: False)
    return setup


def dynamic_record(setup):
    from herdr_engineering.control_plane.service_catalog import service_listener_id
    return {"service_id": service_listener_id("host", 101, "boot:123", 44000),
            "host_id": "host", "source_port": 44000, "service_name": "node", "discovery_source": "native_listener",
            "listener_identity": {"pid": 101, "start": "boot:123"}, "ownership": dict(setup.proof),
            "session_id": setup.proof["session_id"]}


def test_high_dynamic_port_is_an_observation_without_a_forwarded_endpoint(dynamic):
    summary = dynamic.feed.run_once()
    assert summary["listener_observed"] == summary["new_discovered"] == summary["advertised"] == 1
    envelope = dynamic.client.posts[0]
    assert envelope["entity_id"] == dynamic_record(dynamic)["service_id"]
    assert envelope["event_type"] == "service.listener_observed"
    assert envelope["payload"]["source_port"] == 44000
    assert not set(envelope["payload"]) & {"url", "route", "port", "status", "lease_id"}
    assert dynamic.state.read_text() == "{}"
    assert dynamic.feed.run_once()["unchanged"] == 1


@pytest.mark.parametrize("debug", [True, None])
def test_debug_or_uninspectable_listener_never_advertises(dynamic, monkeypatch, debug):
    monkeypatch.setattr(feed_module, "_debug_listener", lambda pid: debug)
    summary = dynamic.feed.run_once()
    assert summary["debug_excluded" if debug else "debug_unknown"] == 1
    assert not dynamic.client.posts


@pytest.mark.parametrize("command,expected", [
    (b"chrome\0--remote-debugging-port=0\0--private-data=hidden\0", True),
    (b"chrome\0--remote-debugging-port\09222\0", True),
    (b"node\0server.js\0", False), (b"", None), (b"x" * 16385, None),
])
def test_private_browser_arguments_only_determine_exclusion(monkeypatch, command, expected):
    monkeypatch.setattr(feed_module.os, "uname", lambda: SimpleNamespace(sysname="Linux"))
    monkeypatch.setattr(feed_module, "open", lambda *args: io.BytesIO(command), raising=False)
    assert feed_module._debug_listener(123) is expected


@pytest.mark.parametrize("complete", [True, False])
def test_only_complete_scan_can_report_observer_owned_listener_absent(dynamic, complete):
    dynamic.client.services = [dynamic_record(dynamic)]
    dynamic.listeners.clear()
    dynamic.listeners.complete = complete
    summary = dynamic.feed.run_once()
    assert summary["listener_missing"] == (1 if complete else 0)
    if complete:
        payload = dynamic.client.posts[0]["payload"]
        assert payload["state"] == "missing" and payload["scan_complete"] is True
        assert "ownership" not in payload
    else:
        assert dynamic.client.posts[0]["payload"]["state"] == "unconfirmed"


def test_ambiguous_pid_does_not_claim_absence_or_new_ownership(dynamic):
    dynamic.client.services = [dynamic_record(dynamic)]
    dynamic.listeners[44000] = {"pid_ambiguous": True}
    summary = dynamic.feed.run_once()
    assert summary["listener_missing"] == summary["listener_observed"] == 0
    assert dynamic.client.posts[0]["payload"]["state"] == "unconfirmed"


def test_registered_debug_listener_never_promotes_current_ownership(setup, monkeypatch):
    monkeypatch.setattr(feed_module, "_debug_listener", lambda pid: True)
    summary = setup.feed.run_once()
    assert summary["owned"] == 0
    assert summary["debug_excluded"] == 1
    assert setup.client.posts[0]["payload"]["state"] == "unconfirmed"
    assert "ownership" not in setup.client.posts[0]["payload"]


def test_unregistered_inventory_rotates_past_unowned_listeners(dynamic):
    dynamic.listeners.clear()
    dynamic.listeners.update({port: {"pid": port} for port in range(40000, 40260)})
    dynamic.listeners[45000] = {"pid": 101}
    dynamic.feed.ownership = SimpleNamespace(classify=lambda pid, **kwargs: dict(dynamic.proof) if pid == 101 else None)
    first, first_summary = dynamic.feed.collect()
    second, second_summary = dynamic.feed.collect()
    assert not first
    assert first_summary["truncated"] is True
    assert second_summary["listener_observed"] == 1
    assert second[0]["payload"]["source_port"] == 45000


def test_legacy_record_is_not_adopted_by_port_without_its_state_mapping(dynamic):
    dynamic.client.services = [{"service_id": "legacy", "host_id": "host", "source_port": 44000, "service_name": "legacy app"}]
    assert dynamic.feed.run_once()["unmapped"] == 1
    assert not dynamic.client.posts


def test_reused_port_gets_new_physical_identity_and_prior_observation_becomes_missing(dynamic, monkeypatch):
    old = dynamic_record(dynamic)
    dynamic.client.services = [old]
    monkeypatch.setattr(feed_module, "_process_identity", lambda pid: {"start": "boot:new"})
    dynamic.proof["listener_start"] = "boot:new"
    summary = dynamic.feed.run_once()
    assert summary["listener_observed"] == summary["listener_missing"] == 1
    observed = [e for e in dynamic.client.posts if e["payload"]["state"] == "observed"][0]
    missing = [e for e in dynamic.client.posts if e["payload"]["state"] == "missing"][0]
    assert observed["entity_id"] != old["service_id"]
    assert missing["entity_id"] == old["service_id"]


def test_dynamic_observation_round_trip_creates_local_only_then_retains_missing_history(dynamic):
    from herdr_engineering.control_plane.ingest import IngestService
    from herdr_engineering.control_plane.repo import ControlPlaneRepo

    repo = ControlPlaneRepo()
    repo.upsert_session("herdr:host:eng:p1", host_id="host", herdr_session="eng", pane_id="p1", terminal_id="term_1")

    class Backend:
        def request(self, route, body=None):
            if route.startswith("/api/v1/services"):
                return {"services": repo.list_services()}
            assert route == "/api/v1/ingest" and body["event_type"] == "service.listener_observed"
            return {"ok": True, "entity": IngestService(repo).ingest(body)}

    dynamic.feed.client = Backend()
    assert dynamic.feed.run_once()["advertised"] == 1
    row = repo.list_services()[0]
    assert row["catalog"]["scope"] == "herdr"
    assert row["url"] is None and row["endpoint_scope"] == "local_only"
    assert row["discovery_source"] == "native_listener"
    origin = copy.deepcopy(row["verified_origin"])
    dynamic.listeners.clear()
    assert dynamic.feed.run_once()["listener_missing"] == 1
    row = repo.list_services()[0]
    assert row["metadata_observation"]["state"] == "missing"
    assert row["status"] == "unknown"
    assert row["verified_origin"] == origin
    assert row["url"] is None


def test_registered_work_reserves_budget_for_dynamic_discovery(setup):
    setup.listeners[44000] = {"pid": 202}

    def classify(pid, **kwargs):
        if pid == 101:
            setup.ticks[0] += 12
        return {**setup.proof, "listener_pid": pid}

    setup.feed.ownership = SimpleNamespace(classify=classify)
    _, summary = setup.feed.collect()
    assert summary["mapped"] == 1
    assert summary["listener_observed"] == 1


def test_feed_asks_only_for_its_own_hosts_services(setup):
    setup.feed.collect()
    assert setup.client.service_routes == ["/api/v1/services?host_id=host"]
