import copy
import json

import pytest

from herdr_engineering.control_plane.bridge import DurableSource, _load_full_into
from herdr_engineering.control_plane.projection_checkpoint import (
    capture_projection,
    restore_projection,
)
from herdr_engineering.control_plane.projection_store import decode_checkpoint, encode_checkpoint
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def replay_repo(*, sink=None):
    repo = ControlPlaneRepo(now=lambda: 1000, sink=sink)
    repo.inspector._clock = lambda: 1000
    return repo


class Sink:
    def __init__(self):
        self.rows = []

    def record(self, event):
        self.rows.append(copy.deepcopy(event))

    record_required = record


class Source(DurableSource):
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    def missions(self):
        return []

    def sessions(self):
        return []

    def hosts(self):
        return []

    def services(self):
        return []

    def events(self):
        return self.rows


def sample():
    sink = Sink()
    repo = replay_repo(sink=sink)
    mission = repo.create_mission("Checkpoint mission")
    repo.upsert_session("agent-one", name="Ada", mission_id=mission)
    repo.get_session("agent-one")
    repo.rename_agent("agent-one", "Grace Hopper", 1)
    repo.upsert_service(
        "host-one", "App", 8000, status="healthy", description="Original description"
    )
    repo.inspector.ingest(
        "agent-one",
        {
            "kind": "message",
            "role": "assistant",
            "text": "Retained private message",
            "message_id": "message-one",
        },
    )
    repo.record_token_usage("project", "model", 11, 7, request_id="usage-one", mission_id=mission)
    return repo, sink


def projected(repo):
    return {
        "missions": repo.list_missions(),
        "sessions": repo.list_sessions(),
        "services": repo.list_services(),
        "usage": repo.token_analytics(),
        "names": repo.agent_identities.list(),
        "inspector": repo.inspector.health(),
    }


def test_compact_checkpoint_roundtrip_preserves_complete_owned_projections_without_writes():
    _, sink = sample()
    reference = replay_repo()
    _load_full_into(reference, Source(sink.rows), capture_event_core=True)
    checkpoint = capture_projection(
        reference,
        reference._checkpoint_event_core,
        coverage={"position": 10, "legacy_rows": 0, "public_rows": len(reference._event_order)},
    )
    packed, checksum = encode_checkpoint(checkpoint)
    restored = replay_repo(sink=Sink())
    restore_projection(
        restored,
        decode_checkpoint(packed, checksum),
        full_streams={},
        tail_rows=[],
        structured={name: [] for name in ("missions", "sessions", "hosts", "services")},
        coverage=checkpoint["coverage"],
    )
    assert projected(restored) == projected(reference)
    assert restored._sink.rows == []
    assert all(
        len(stream) == 1
        for key, stream in checkpoint["repo"]["events"].items()
        if key.startswith(("service|", "session|", "host|"))
    )
    assert restored.get_session("agent-one")["display_name"] == "Grace Hopper"
    assert "Retained private message" in json.dumps(restored.inspector.export_checkpoint())
    assert not any(e["entity_type"].startswith("agent_") for e in restored.recent_events())


def test_dirty_core_stream_reconstruction_preserves_fields_and_legacy_history():
    live, sink = sample()
    prefix = copy.deepcopy(sink.rows)
    reference = replay_repo()
    _load_full_into(reference, Source(prefix), capture_event_core=True)
    checkpoint = capture_projection(
        reference,
        reference._checkpoint_event_core,
        coverage={"position": 10, "legacy_rows": 0, "public_rows": len(reference._event_order)},
    )
    service_id = next(iter(live._services))
    live.upsert_service("host-one", "App", 8000, status="unavailable")
    final = replay_repo()
    _load_full_into(final, Source(sink.rows), capture_event_core=True)
    dirty = {
        ("service", service_id): [
            copy.deepcopy(row)
            for row in sink.rows
            if row["entity_type"] == "service" and row["entity_id"] == service_id
        ]
    }
    restored = replay_repo()
    restore_projection(
        restored,
        checkpoint,
        full_streams=dirty,
        tail_rows=sink.rows[len(prefix) :],
        structured={name: [] for name in ("missions", "sessions", "hosts", "services")},
        coverage={"position": 11, "legacy_rows": 0, "public_rows": len(final._event_order)},
    )
    assert projected(restored) == projected(final)
    assert restored.get_service(service_id)["description"] == "Original description"


def assert_domain_equivalence(actual, expected):
    """Compare durable views; inspector epochs identify processes, not state."""
    assert projected(actual) == projected(expected)
    for session in ("agent-one", "quiet-agent"):
        for role in ("viewer", "operator"):
            left = actual.inspector.query(session, role)
            right = expected.inspector.query(session, role)
            assert {k: v for k, v in left.items() if k != "epoch"} == {
                k: v for k, v in right.items() if k != "epoch"
            }
    assert actual.host_metrics.export_checkpoint() == expected.host_metrics.export_checkpoint()
    assert actual.ledger.export_checkpoint() == expected.ledger.export_checkpoint()
    assert actual.stage_analytics() == expected.stage_analytics()
    assert actual.list_test_runs() == expected.list_test_runs()
    assert actual.graph.snapshot() == expected.graph.snapshot()
    assert actual.deployments.snapshot() == expected.deployments.snapshot()
    assert actual.deployment_requests.pending() == expected.deployment_requests.pending()
    assert actual._collab_replay == expected._collab_replay
    assert actual.collab_quarantine == expected.collab_quarantine
    assert actual.collab_quarantine_reasons == expected.collab_quarantine_reasons


def domain_rows():
    """Use real writers, as in the bridge and owner-codec replay fixtures."""
    from herdr_engineering.control_plane.ingest import IngestService

    live, sink = sample()
    ingest = IngestService(live)
    live.upsert_mission("M1", source="pi", title="Stages", stage="planning")
    live.set_mission_stage("pi:M1", "implementation")
    live.inspector.ingest("quiet-agent", {"kind": "message", "role": "user", "text": "quiet"})
    live.inspector.ingest(
        "agent-one",
        {
            "events": [
                {"kind": "tool", "phase": "start", "call_id": "c", "tool": "bash"},
                {"kind": "test", "phase": "start", "run_id": "r"},
                {"kind": "usage", "input_tokens": 12, "output_tokens": 3},
                {"kind": "reasoning", "text": "private reasoning"},
            ]
        },
    )
    live.host_metrics.observe_state("host-one", {"maintenance": True}, {"cpu": 12})
    ingest.ingest(
        {
            "source": "pi",
            "entity_type": "test_run",
            "entity_id": "run1",
            "event_type": "test_run.started",
            "payload": {"mission_id": "M1", "status": "running"},
        }
    )
    principal = {"kind": "agent", "id": "key:writer", "authenticated_by": "api_key"}
    envelope = {
        "schema_version": 2,
        "event_id": "graph-1",
        "source": "pi",
        "source_sequence": 1,
        "entity_type": "lineage",
        "entity_id": "task:t1",
        "event_type": "node.upsert",
        "visibility_scope": "org",
        "payload": {"node_type": "task", "node_id": "task:t1", "label": "Original"},
    }
    ingest.ingest(envelope, principal)
    live._persist_host_metrics(
        {
            "entity_type": "host_metrics",
            "entity_id": "host-one",
            "event_type": "HostMetricsRollup",
            "sequence": 1,
            "payload": {"rollup": {"ts": 600, "cpu": 12, "n": 1}},
        }
    )
    live.record_event(
        "collab",
        "log",
        "message.created",
        {
            "org_id": "o",
            "correlation_id": "c",
            "created_at": live._stamp(),
            "text": "private prefix",
        },
    )
    live.record_event("collab", "foreign", "foreign", {"text": "quarantined"})
    deployment = {
        "id": "1" * 32,
        "ts": "1970-01-01T00:16:40Z",
        "host": "login2.hive",
        "component": "hive-gateway",
        "step": "deployment.hive-gateway.cycle",
        "outcome": "started",
        "from": "a" * 40,
        "to": "b" * 40,
        "evidence": "",
    }
    live.deployments.ingest(deployment, reported_by="hive")
    live.deployment_requests.add(
        "component", "hive-nodes", {"hive-nodes": ("b" * 40, 1)}, "operator:x"
    )
    prefix = copy.deepcopy(sink.rows)
    live.deployments.ingest({**deployment, "id": "2" * 32, "outcome": "ok"}, reported_by="hive")
    live._persist_host_metrics(
        {
            "entity_type": "host_metrics",
            "entity_id": "host-one",
            "event_type": "HostMetricsRollup",
            "sequence": 2,
            "payload": {"rollup": {"ts": 900, "cpu": 30, "n": 1}},
        }
    )
    live.record_event(
        "collab",
        "log",
        "message.created",
        {"org_id": "o", "correlation_id": "d", "created_at": live._stamp(), "text": "private tail"},
    )
    live.rename_agent("agent-one", "Katherine Johnson", 2)
    live.set_mission_stage("pi:M1", "review")
    live.inspector.ingest(
        "agent-one",
        {
            "events": [
                {
                    "kind": "tool",
                    "phase": "end",
                    "call_id": "c",
                    "exit_code": 0,
                    "output": "private output",
                },
                {"kind": "test", "phase": "end", "run_id": "r", "passed": 3},
                {"kind": "usage", "input_tokens": 5, "output_tokens": 2},
            ]
        },
    )
    live.host_metrics.observe_state("host-one", {"maintenance": False}, {"cpu": 30})
    ingest.ingest(
        {
            "source": "pi",
            "entity_type": "test_run",
            "entity_id": "run1",
            "event_type": "test_run.completed",
            "payload": {"mission_id": "M1", "passed": 2},
        }
    )
    ingest.ingest(
        {
            **envelope,
            "event_id": "graph-4",
            "source_sequence": 4,
            "payload": {**envelope["payload"], "label": "Updated"},
        },
        principal,
    )
    return prefix, sink.rows


def test_dirty_private_and_noncore_domains_match_full_replay_without_writes():
    prefix, rows = domain_rows()
    reference = replay_repo()
    _load_full_into(reference, Source(prefix), capture_event_core=True)
    coverage = {
        "position": len(prefix),
        "legacy_rows": 0,
        "public_rows": len(reference._event_order),
    }
    packed, checksum = encode_checkpoint(
        capture_projection(reference, reference._checkpoint_event_core, coverage=coverage)
    )
    tail = rows[len(prefix) :]
    dirty = {(r["entity_type"], r["entity_id"]) for r in tail}
    streams = {key: [r for r in rows if (r["entity_type"], r["entity_id"]) == key] for key in dirty}
    expected = replay_repo()
    _load_full_into(expected, Source(rows), capture_event_core=True)
    restored = replay_repo(sink=Sink())
    restore_projection(
        restored,
        decode_checkpoint(packed, checksum),
        full_streams=streams,
        tail_rows=tail,
        structured={name: [] for name in ("missions", "sessions", "hosts", "services")},
        coverage={**coverage, "position": len(rows)},
    )
    assert_domain_equivalence(restored, expected)
    assert restored.get_session("agent-one")["display_name"] == "Katherine Johnson"
    assert restored.graph.snapshot()["nodes"]
    assert restored.list_test_runs()
    assert restored.deployments.snapshot()["components"]["hive-gateway"]["timeline"]
    assert restored.deployment_requests.pending()
    assert len(restored._collab_replay) == 2
    assert restored.collab_quarantine_reasons == ["foreign_stream"]
    assert restored.host_metrics.rollup_count("host-one") == 2
    assert restored.inspector.query("quiet-agent", "operator")["events"]
    assert restored._sink.rows == []


def test_backdated_inspector_completion_preserves_full_replay_stats():
    """A late completion can order before the prefix by its receive timestamp."""
    prefix, rows = domain_rows()
    # Model legacy completion rows without _received_at, arriving after the
    # checkpoint but carrying an expired ingest timestamp.
    tail = copy.deepcopy(rows[len(prefix) :])
    for row in tail:
        if row["event_type"] == "inspector.test":
            row["payload"].pop("_received_at", None)
            row["ingest_timestamp"] = "1969-12-30T00:00:00+00:00"
    reference = replay_repo()
    _load_full_into(reference, Source(prefix), capture_event_core=True)
    coverage = {
        "position": len(prefix),
        "legacy_rows": 0,
        "public_rows": len(reference._event_order),
    }
    checkpoint = capture_projection(reference, reference._checkpoint_event_core, coverage=coverage)
    all_rows = prefix + tail
    dirty = {(r["entity_type"], r["entity_id"]) for r in tail}
    streams = {
        key: [r for r in all_rows if (r["entity_type"], r["entity_id"]) == key] for key in dirty
    }
    expected = replay_repo()
    _load_full_into(expected, Source(all_rows), capture_event_core=True)
    actual = replay_repo()
    restore_projection(
        actual,
        checkpoint,
        full_streams=streams,
        tail_rows=tail,
        structured={name: [] for name in ("missions", "sessions", "hosts", "services")},
        coverage={**coverage, "position": len(all_rows)},
    )
    for role in ("viewer", "operator"):
        actual_view = actual.inspector.query("agent-one", role)
        expected_view = expected.inspector.query("agent-one", role)
        assert {k: v for k, v in actual_view.items() if k != "epoch"} == {
            k: v for k, v in expected_view.items() if k != "epoch"
        }
    assert actual.inspector.health() == expected.inspector.health()


def recover_rows_from_checkpoint(prefix, tail):
    reference = replay_repo()
    _load_full_into(reference, Source(prefix), capture_event_core=True)
    coverage = {
        "position": len(prefix),
        "legacy_rows": 0,
        "public_rows": len(reference._event_order),
    }
    packed, checksum = encode_checkpoint(
        capture_projection(reference, reference._checkpoint_event_core, coverage=coverage)
    )
    rows = prefix + tail
    dirty = {(r["entity_type"], r["entity_id"]) for r in tail}
    streams = {key: [r for r in rows if (r["entity_type"], r["entity_id"]) == key] for key in dirty}
    expected = replay_repo()
    _load_full_into(expected, Source(rows), capture_event_core=True)
    actual = replay_repo(sink=Sink())
    restore_projection(
        actual,
        decode_checkpoint(packed, checksum),
        full_streams=streams,
        tail_rows=tail,
        structured={name: [] for name in ("missions", "sessions", "hosts", "services")},
        coverage={**coverage, "position": len(rows)},
    )
    assert actual._sink.rows == []
    return actual, expected


@pytest.mark.parametrize("dirty_host", [False, True], ids=["checkpoint-only", "dirty-host"])
def test_host_operator_policy_survives_later_lower_revision_reports(dirty_host):
    sink = Sink()
    live = replay_repo(sink=sink)
    reported = {
        "host_name": "Host one",
        "status": "healthy",
        "scheduler_policy": {"revision": 0, "mode": "manual_only"},
    }
    live.record_event("host", "h1", "HostReported", reported)
    live.record_event("host", "h1", "HostPolicyChanged", {"revision": 2, "mode": "disabled"})
    live.record_event("host", "h1", "HostReported", reported)
    prefix = copy.deepcopy(sink.rows)
    if dirty_host:
        live.record_event("host", "h1", "HostReported", {**reported, "status": "unavailable"})
    actual, expected = recover_rows_from_checkpoint(prefix, sink.rows[len(prefix) :])
    assert expected.get_host("h1")["scheduler_policy"] == {"revision": 2, "mode": "disabled"}
    assert actual.get_host("h1")["scheduler_policy"] == expected.get_host("h1")["scheduler_policy"]
    assert actual.get_host("h1") == expected.get_host("h1")
    if dirty_host:
        assert actual.get_host("h1")["status"] == "unavailable"


@pytest.mark.parametrize("kind", ["reservation", "lease"])
@pytest.mark.parametrize("existing", [True, False], ids=["existing-entry", "new-tail-entry"])
def test_scheduler_released_tail_replaces_checkpoint_state(kind, existing):
    sink = Sink()
    live = replay_repo(sink=sink)
    save = getattr(live, "save_scheduler_" + kind)
    field = kind + "_id"
    # An unaffected entry must survive reconstruction of the affected stream.
    save({field: "quiet", "state": "committed", "generation": 1})
    if existing:
        save({field: "changed", "state": "running", "generation": 1})
    prefix = copy.deepcopy(sink.rows)
    released = {field: "changed", "state": "released", "generation": 2}
    save(released)
    actual, expected = recover_rows_from_checkpoint(prefix, sink.rows[len(prefix) :])
    listing = "list_scheduler_" + ("reservations" if kind == "reservation" else "leases")
    expected_rows = {row[field]: row for row in getattr(expected, listing)()}
    actual_rows = {row[field]: row for row in getattr(actual, listing)()}
    assert expected_rows["changed"] == released
    assert actual_rows == expected_rows
    assert actual_rows["quiet"]["state"] == "committed"


def test_dirty_inspector_session_replays_authoritative_global_retention_history():
    """An unchanged _global stream still determines a dirty session's retention."""
    now = 1_800_000_000
    clock = [now - 48 * 3600 - 1]

    def repository(*, sink=None):
        repo = ControlPlaneRepo(
            now=lambda: clock[0], sink=sink, inspector_limits={"retention_hours": 720}
        )
        repo.inspector._clock = lambda: clock[0]
        return repo

    sink = Sink()
    live = repository(sink=sink)
    live.inspector.set_prefs({"retention_hours": 72})
    clock[0] += 1
    assert (
        live.inspector.ingest(
            "s1", {"kind": "message", "role": "user", "text": "48-hour-old message"}
        )["accepted"]
        == 1
    )
    prefix = copy.deepcopy(sink.rows)
    clock[0] = now
    reference = repository()
    _load_full_into(reference, Source(prefix), capture_event_core=True)
    assert len(reference.inspector.query("s1", "operator")["events"]) == 1
    coverage = {
        "position": len(prefix),
        "legacy_rows": 0,
        "public_rows": len(reference._event_order),
    }
    packed, checksum = encode_checkpoint(
        capture_projection(reference, reference._checkpoint_event_core, coverage=coverage)
    )
    assert (
        live.inspector.ingest("s1", {"kind": "message", "role": "user", "text": "current message"})[
            "accepted"
        ]
        == 1
    )
    tail = sink.rows[len(prefix) :]
    assert {(row["entity_type"], row["entity_id"]) for row in tail} == {("inspector", "s1")}
    # The database dirty-stream query excludes unchanged _global. Recovery must
    # fetch its authoritative preference history and replay it in receive order;
    # blindly seeding the latest preference cannot reproduce historical policy.
    streams = {("inspector", "s1"): [row for row in sink.rows if row["entity_id"] == "s1"]}
    expected = repository()
    _load_full_into(expected, Source(sink.rows), capture_event_core=True)
    restored = repository(sink=Sink())
    restore_projection(
        restored,
        decode_checkpoint(packed, checksum),
        full_streams=streams,
        tail_rows=tail,
        structured={name: [] for name in ("missions", "sessions", "hosts", "services")},
        coverage={**coverage, "position": len(sink.rows)},
    )
    assert (
        restored.inspector.raw_prefs() == expected.inspector.raw_prefs() == {"retention_hours": 72}
    )
    expected_events = expected.inspector.query("s1", "operator")["events"]
    assert [event["data"]["text"] for event in expected_events] == [
        "current message",
        "48-hour-old message",
    ]
    actual_events = restored.inspector.query("s1", "operator")["events"]
    assert actual_events == expected_events
    assert restored._sink.rows == []
