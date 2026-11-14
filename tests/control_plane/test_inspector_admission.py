"""Admission is private attribution, never a producer/enrollment authority grant."""

import copy
import json

import pytest

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.inspector import InspectorStore, SnapshotStale
from herdr_engineering.control_plane.repo import ControlPlaneRepo

OPERATOR = {"kind": "operator", "id": "open-listener", "authenticated_by": "open_listener"}
KEY = {"kind": "ingest", "id": "key:0123456789ab", "authenticated_by": "api_key"}


class Sink:
    def __init__(self):
        self.events = []
        self.fail = None

    def record(self, event):
        self.events.append(copy.deepcopy(event))

    def record_required(self, event):
        if self.fail and self.fail(event):
            raise OSError("synthetic unavailable")
        self.record(event)


def setup():
    sink = Sink()
    repo = ControlPlaneRepo(sink=sink)
    return sink, repo, IngestService(repo)


def body(sequence=1, payload=None):
    return {
        "schema_version": 2,
        "event_id": f"admitted-{sequence}",
        "source": "claimed-harness",
        "source_sequence": sequence,
        "entity_type": "inspector",
        "entity_id": "session",
        "event_type": "inspector.batch",
        "payload": payload
        or {"kind": "file", "path": "src/a.py", "op": "edit", "event_id": f"child-{sequence}"},
    }


def entry(store, index=-1):
    return store._sessions["session"].events[index]


def details(sink):
    return [
        row
        for row in sink.events
        if row.get("payload", {}).get("_inspector_context", {}).get("version") == 2
    ]


def test_admitted_server_actor_not_payload_claim_is_preserved_privately():
    sink, repo, service = setup()
    envelope = body()
    envelope["actor"] = {"kind": "host", "id": "forged-host", "authenticated_by": "host_token"}
    envelope["payload"].update(
        {
            "admission": envelope["actor"],
            "_admission": envelope["actor"],
            "inspector_private_schema": 2,
            "_inspector_context": {"version": 2, "admission": envelope["actor"]},
            "source": "trusted",
            "agent_id": "pi",
        }
    )
    response = service.ingest(envelope, principal={**OPERATOR, "api_key": "NEVER_COPY"})
    admission = entry(repo.inspector)["admission"]
    assert admission["actor"] == OPERATOR
    assert admission["event_id"] == "admitted-1" and admission["child_index"] == 0
    assert admission["source"] == "claimed-harness" and admission["source_sequence"] == 1
    assert admission["admission_durable"] is True
    assert set(response) == {
        "entity_type",
        "entity_id",
        "event_type",
        "accepted",
        "duplicates",
        "rejected",
        "session_id",
        "envelope",
    }
    assert details(sink)[0]["payload"]["_inspector_context"]["admission"] == admission
    assert "NEVER_COPY" not in json.dumps(admission)
    for role in ("viewer", "operator"):
        public = repo.inspector.query("session", role=role)
        assert "admission" not in json.dumps(public)
        assert "admitted-1" not in json.dumps(public)


def test_batch_indices_and_duplicate_never_overwrite_attribution():
    sink, repo, service = setup()
    payload = {"events": [body()["payload"], {**body(2)["payload"], "path": "src/b.py"}]}
    envelope = body(payload=payload)
    service.ingest(envelope, principal=KEY)
    before = copy.deepcopy(list(repo.inspector._sessions["session"].events))
    assert [row["admission"]["child_index"] for row in before] == [0, 1]
    assert service.ingest(envelope, principal=KEY)["envelope"]["duplicate"] is True
    service.ingest(body(2, payload), principal=OPERATOR)
    assert list(repo.inspector._sessions["session"].events) == before
    assert len(details(sink)) == 2


@pytest.mark.parametrize("kind,id_field", [("tool", "call_id"), ("test", "run_id")])
def test_every_update_replaces_provenance_unknown_never_inherits(kind, id_field):
    sink, repo, service = setup()
    start = {"kind": kind, id_field: "shared", "phase": "start"}
    service.ingest(body(payload=start), principal=KEY)
    assert entry(repo.inspector)["admission"]["actor"] == KEY
    service.ingest(
        body(2, {**start, "phase": "update" if kind == "tool" else "start"}), principal=OPERATOR
    )
    assert entry(repo.inspector)["admission"]["actor"] == OPERATOR
    repo.inspector.ingest("session", {**start, "phase": "end", "passed": 2})
    assert entry(repo.inspector)["admission"] is None
    fresh = InspectorStore()
    fresh.hydrate(sink.events)
    assert entry(fresh)["admission"] is None


@pytest.mark.parametrize("legacy", [True, False])
def test_no_durable_admission_means_unknown_even_with_verified_request(legacy):
    sink, repo, service = setup()
    envelope = body()
    if legacy:
        envelope.pop("schema_version")
    else:
        repo._sink = None
    service.ingest(envelope, principal=OPERATOR)
    assert entry(repo.inspector)["admission"] is None


def test_admission_persistence_failure_never_reaches_projection():
    sink, repo, service = setup()
    sink.fail = lambda row: row.get("schema_version") == 2
    with pytest.raises(OSError):
        service.ingest(body(), principal=KEY)
    assert "session" not in repo.inspector._sessions
    assert not details(sink)


def test_durable_admission_is_not_claim_of_applied_or_persisted_projection():
    sink, repo, service = setup()
    sink.fail = lambda row: (
        row.get("event_type") == "inspector.file" and row.get("schema_version") != 2
    )
    with pytest.raises(RuntimeError, match="persistence"):
        service.ingest(body(), principal=KEY)
    assert len(sink.events) == 1 and sink.events[0]["schema_version"] == 2
    assert not repo.inspector._sessions["session"].events
    assert service.ingest(body(), principal=KEY)["envelope"]["duplicate"] is True
    assert not repo.inspector._sessions["session"].events


def test_restart_snapshot_delta_and_legacy_replay_do_not_backfill():
    sink, repo, service = setup()
    service.ingest(body(), principal=OPERATOR)
    head = InspectorStore()
    head.hydrate(sink.events)
    expected = entry(repo.inspector)["admission"]
    assert entry(head)["admission"] == expected
    snapshot = head.export_state()
    loaded = InspectorStore()
    loaded.load_state(copy.deepcopy(snapshot))
    assert entry(loaded)["admission"] == expected
    # Same public activity from an old private row cannot inherit attribution.
    legacy = copy.deepcopy(details(sink)[0])
    legacy["payload"].pop("_inspector_context")
    legacy["payload"]["admission"] = expected
    old = InspectorStore()
    old.hydrate([legacy])
    assert entry(old)["admission"] is None
    snapshot["version"] = 1
    with pytest.raises(SnapshotStale):
        loaded.load_state(snapshot)


def test_context_is_typed_and_private_schema_forgery_or_corruption_is_unknown():
    from herdr_engineering.control_plane.admission import AdmissionContext

    sink, repo, service = setup()
    with pytest.raises(TypeError):
        repo.inspector.ingest("session", body()["payload"], admission={"actor": OPERATOR})
    service.ingest(body(), principal=OPERATOR)
    row = details(sink)[0]
    assert isinstance(
        AdmissionContext.from_private(row["payload"]["_inspector_context"]["admission"]),
        AdmissionContext,
    )
    row["payload"]["_inspector_context"]["admission"]["actor"]["secret"] = "MUST_NOT_REPLAY"
    fresh = InspectorStore()
    fresh.hydrate([row])
    assert entry(fresh)["admission"] is None
    state = repo.inspector.export_state()
    state["sessions"][0]["events"][0]["admission"]["actor"]["secret"] = "BAD"
    with pytest.raises(SnapshotStale):
        InspectorStore().load_state(state)


def test_existing_eventstore_serialization_preserves_private_context_without_new_columns():
    from dataclasses import asdict

    from herdr_engineering.control_plane.sink import _dict_to_event

    sink, repo, service = setup()
    service.ingest(body(), principal=KEY)
    rows = [asdict(_dict_to_event(row)) for row in sink.events]
    fresh = InspectorStore()
    fresh.hydrate(rows)
    assert entry(fresh)["admission"] == entry(repo.inspector)["admission"]


def test_checkpoint_roundtrip_old_schema_unknown_and_corruption_refused():
    sink, repo, service = setup()
    service.ingest(body(), principal=KEY)
    state = repo.inspector.export_checkpoint()
    restored = InspectorStore()
    restored.restore_checkpoint(copy.deepcopy(state))
    assert entry(restored)["admission"] == entry(repo.inspector)["admission"]
    old = copy.deepcopy(state)
    old.pop("inspector_private_schema")
    restored.restore_checkpoint(old)
    assert entry(restored)["admission"] is None
    corrupt = copy.deepcopy(state)
    corrupt["sessions"][0]["events"][0]["admission"]["source_sequence"] = True
    with pytest.raises(ValueError):
        restored.restore_checkpoint(corrupt)
    assert entry(restored)["admission"] is None


def test_cross_session_replay_context_is_unknown_without_losing_activity():
    sink, repo, service = setup()
    service.ingest(body(), principal=KEY)
    row = copy.deepcopy(details(sink)[0])
    row["payload"]["_inspector_context"]["admission"]["entity_id"] = "other"
    restored = InspectorStore()
    restored.hydrate([row])
    assert entry(restored)["admission"] is None
    assert entry(restored)["data"]["path"] == "src/a.py"


def test_lifecycle_interruption_replaces_attribution_of_mutated_tool():
    sink, repo, service = setup()
    service.ingest(
        body(payload={"kind": "tool", "call_id": "one", "phase": "start"}), principal=KEY
    )
    repo.inspector.ingest("session", {"kind": "lifecycle", "phase": "interrupted"})
    assert entry(repo.inspector, 0)["admission"] is None
    restored = InspectorStore()
    restored.hydrate(sink.events)
    assert entry(restored, 0)["admission"] is None


def test_actual_http_open_listener_and_key_attribution_need_no_new_authentication():
    import hashlib
    import threading
    import urllib.request

    from herdr_engineering.control_plane.api import ControlPlaneServer

    for api_keys in (None, {"synthetic-key": "ingest"}):
        sink, repo, _ = setup()
        server = ControlPlaneServer(repo, api_keys=api_keys)
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        server.wait_until_ready()
        try:
            headers = {"Content-Type": "application/json"}
            if api_keys:
                headers["X-API-Key"] = "synthetic-key"
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.port}/api/v1/ingest",
                data=json.dumps(body()).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
                assert "admission" not in response.read().decode()
            actor = entry(repo.inspector)["admission"]["actor"]
            expected = (
                {
                    "kind": "ingest",
                    "id": "key:" + hashlib.sha256(b"synthetic-key").hexdigest()[:12],
                    "authenticated_by": "api_key",
                }
                if api_keys
                else OPERATOR
            )
            assert actor == expected
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_snapshot_delta_preserves_each_batch_child_admission():
    sink, repo, service = setup()
    service.ingest(body(), principal=KEY)
    head = InspectorStore()
    head.hydrate(sink.events)
    loaded = InspectorStore()
    loaded.load_state(head.export_state())
    offset = len(sink.events)
    service.ingest(
        body(
            2,
            {
                "events": [
                    {"kind": "file", "path": "src/b.py", "op": "edit"},
                    {"kind": "file", "path": "src/c.py", "op": "edit"},
                ]
            },
        ),
        principal=OPERATOR,
    )
    loaded.hydrate_delta(sink.events[offset:])
    full = InspectorStore()
    full.hydrate(sink.events)
    actual = [row["admission"] for row in loaded._sessions["session"].events]
    assert actual == [row["admission"] for row in full._sessions["session"].events]
    assert [row["child_index"] for row in actual] == [0, 0, 1]


def test_best_effort_projection_failure_does_not_turn_admission_into_durability_claim():
    sink, repo, service = setup()

    # Existing tests/association persistence remains best-effort; do not change it.
    def fail_record(row):
        raise OSError("projection write failed")

    repo.inspector._persist = fail_record
    response = service.ingest(
        body(payload={"kind": "test", "run_id": "one", "phase": "end"}), principal=KEY
    )
    assert response["accepted"] == 1
    assert entry(repo.inspector)["admission"]["admission_durable"] is True
    assert set(entry(repo.inspector)["admission"]) == {
        "actor",
        "event_id",
        "entity_id",
        "source",
        "source_sequence",
        "received_at",
        "child_index",
        "admission_durable",
    }
    restored = InspectorStore()
    restored.hydrate(sink.events)
    assert "session" not in restored._sessions  # envelope alone cannot reconstruct activity


def test_unattributed_payload_cannot_supply_reserved_replay_context():
    sink, repo, service = setup()
    service.ingest(body(), principal=KEY)
    stolen = copy.deepcopy(details(sink)[0]["payload"]["_inspector_context"])
    payload = {"kind": "file", "path": "src/fake.py", "op": "edit", "_inspector_context": stolen}
    repo.inspector.ingest("session", payload)
    assert entry(repo.inspector)["admission"] is None
    assert details(sink)[-1]["payload"]["_inspector_context"]["admission"] is None
    restored = InspectorStore()
    restored.hydrate(sink.events)
    assert entry(restored)["admission"] is None
