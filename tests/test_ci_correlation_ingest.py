"""CI links survive real lifecycle ingestion and durable event reconstruction."""
import copy

import pytest

from herdr_engineering.control_plane.bridge import load_into
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.project_links import annotate_ci
from herdr_engineering.control_plane.repo import ControlPlaneRepo

SHA = "a" * 40
OTHER_SHA = "b" * 64


class DurableFixture:
    def __init__(self):
        self.rows = []

    def record(self, event):
        self.rows.append(copy.deepcopy(event))

    def record_required(self, event):
        self.record(event)

    def events(self):
        return self.rows

    def missions(self):
        return []

    def sessions(self):
        return []

    def hosts(self):
        return []

    def services(self):
        return []


def event(kind, entity_id, payload, source="herdr"):
    return {"entity_type": kind, "entity_id": entity_id, "event_type": f"{kind}.updated",
            "source": source, "payload": payload}


def ci_view(sha=SHA):
    return {"repos": [{"id": 1, "repository": "owner/app", "forge_url": "https://github.com/Owner/App"}],
            "pipelines": {"1": [{"repo_id": 1, "revision": sha, "repository": "owner/app"}]}}


def linked(repo, sha=SHA):
    return annotate_ci(ci_view(sha), repo)["pipelines"]["1"][0]


def test_actual_ingest_links_exact_ci_repo_commit_and_replays_namespaces():
    source = DurableFixture()
    repo = ControlPlaneRepo(sink=source)
    ingest = IngestService(repo)
    ingest.ingest(event("mission", "same-id", {"repository_id": "github.com/Owner/App", "commit_sha": SHA.upper()}, "pi"))
    ingest.ingest(event("mission", "same-id", {"forge_url": "https://other.example/owner/app", "commit_sha": SHA}))
    ingest.ingest(event("session", "host:session", {"repo_url": "git@github.com:Owner/App.git", "commit_sha": SHA,
                                                  "revision": 129, "mission_id": "same-id"}, "pi"))
    before = linked(repo)
    assert before["related_sessions"] == [{"session_id": "host:session"}]
    assert before["related_missions"] == [{"mission_id": "same-id", "source": "pi", "mission_key": "pi:same-id"}]
    assert repo.get_session("host:session")["revision"] == 129
    assert repo.get_session("host:session")["commit_sha"] == SHA
    # Heartbeats omit identity and must preserve the explicitly advertised pair.
    ingest.ingest(event("session", "host:session", {"revision": 130}))
    ingest.ingest(event("mission", "same-id", {"stage": "testing"}, "pi"))
    restarted = ControlPlaneRepo()
    load_into(restarted, source)
    after = linked(restarted)
    assert after["related_sessions"] == before["related_sessions"]
    assert after["related_missions"] == before["related_missions"]
    assert restarted.get_mission("pi:same-id")["mission_id"] == "same-id"
    assert restarted.get_session("host:session")["revision"] == 130
    assert restarted.get_session("host:session")["commit_sha"] == SHA


@pytest.mark.parametrize("field", ["repository_id", "forge_url", "repository_url", "repo_url", "clone_url"])
def test_explicit_identity_aliases_are_canonicalized(field):
    repo = ControlPlaneRepo()
    IngestService(repo).ingest(event("session", "s", {field: "https://github.com/OWNER/APP.git", "commit_sha": SHA}))
    assert repo.get_session("s")["repository_id"] == "github.com/owner/app"
    assert linked(repo)["related_sessions"] == [{"session_id": "s"}]


def test_sha256_updates_replace_identity_without_touching_sequence_revision():
    source = DurableFixture()
    repo = ControlPlaneRepo(sink=source)
    ingest = IngestService(repo)
    ingest.ingest(event("session", "s", {"repository_id": "github.com/owner/app", "commit_sha": SHA, "revision": 17}))
    ingest.ingest(event("session", "s", {"commit_sha": OTHER_SHA.upper(), "revision": 18}))
    ingest.ingest(event("mission", "m", {"repository_id": "github.com/owner/app", "commit_sha": OTHER_SHA}))
    assert not linked(repo)["related_sessions"]
    assert linked(repo, OTHER_SHA)["related_sessions"] == [{"session_id": "s"}]
    restarted = ControlPlaneRepo()
    load_into(restarted, source)
    assert linked(restarted, OTHER_SHA)["related_missions"] == [{"mission_id": "m", "source": "herdr", "mission_key": "m"}]
    assert restarted.get_session("s")["revision"] == 18


@pytest.mark.parametrize("payload", [
    {"repository_id": "github.com/owner/app", "commit_sha": "123abc"},
    {"repository_id": "github.com/owner/app", "commit_sha": 42},
    {"repository_id": "github.com/owner/app", "forge_url": "https://other.example/owner/app"},
    {"repository_id": "owner/app"},
])
def test_invalid_or_conflicting_advertisements_do_not_mutate_identity(payload):
    repo = ControlPlaneRepo()
    with pytest.raises(ValueError):
        IngestService(repo).ingest(event("session", "s", payload))
    assert repo.get_session("s") is None


def test_cwd_branch_ports_and_numeric_revision_cannot_create_ci_link():
    repo = ControlPlaneRepo()
    IngestService(repo).ingest(event("session", "s", {"repository_id": "github.com/owner/app",
                                                   "cwd": "/owner/app", "branch": SHA, "revision": 91, "port": 49199}))
    assert not linked(repo)["related_sessions"]


def test_even_hex_session_revision_is_not_a_commit_advertisement():
    repo = ControlPlaneRepo()
    IngestService(repo).ingest(event("session", "s", {"repository_id": "github.com/owner/app", "revision": SHA}))
    assert not linked(repo)["related_sessions"]


@pytest.mark.parametrize("kind", ["session", "mission"])
def test_repository_change_clears_old_commit_live_and_after_replay(kind):
    source = DurableFixture()
    repo = ControlPlaneRepo(sink=source)
    ingest = IngestService(repo)
    ingest.ingest(event(kind, "entity", {"repository_id": "github.com/owner/old", "commit_sha": SHA}))
    ingest.ingest(event(kind, "entity", {"repository_id": "github.com/owner/app"}))
    getter = repo.get_session if kind == "session" else repo.get_mission
    assert getter("entity")["commit_sha"] is None
    assert not linked(repo)["related_" + ("sessions" if kind == "session" else "missions")]
    restarted = ControlPlaneRepo()
    load_into(restarted, source)
    getter = restarted.get_session if kind == "session" else restarted.get_mission
    assert getter("entity")["repository_id"] == "github.com/owner/app"
    assert getter("entity")["commit_sha"] is None
    assert not linked(restarted)["related_" + ("sessions" if kind == "session" else "missions")]
    ingest.ingest(event(kind, "entity", {"repository_id": "github.com/owner/app", "commit_sha": SHA}))
    assert linked(repo)["related_" + ("sessions" if kind == "session" else "missions")]


def test_session_namespace_survives_heartbeat_and_explicit_reassignment():
    source = DurableFixture()
    repo = ControlPlaneRepo(sink=source)
    ingest = IngestService(repo)
    ingest.ingest(event("mission", "same", {}, "herdr"))
    ingest.ingest(event("mission", "same", {}, "pi"))
    ingest.ingest(event("session", "s", {"mission_id": "same"}, "pi"))
    ingest.ingest(event("session", "s", {"revision": 19}))
    assert repo.get_session("s")["mission_key"] == "pi:same"
    assert repo.get_session("s")["mission_source"] == "pi"
    restarted = ControlPlaneRepo()
    load_into(restarted, source)
    assert restarted.get_mission("same")["sessions"] == []
    assert restarted.get_mission("pi:same")["sessions"] == ["s"]
    assert restarted.get_session("s")["mission_key"] == "pi:same"
    ingest.ingest(event("session", "s", {"mission_id": "same"}, "herdr"))
    assert repo.get_mission("pi:same")["sessions"] == []
    assert repo.get_mission("same")["sessions"] == ["s"]
    load_into(restarted, source)
    assert restarted.get_mission("pi:same")["sessions"] == []
    assert restarted.get_mission("same")["sessions"] == ["s"]


def test_session_before_mission_keeps_explicit_namespace():
    repo = ControlPlaneRepo()
    ingest = IngestService(repo)
    ingest.ingest(event("session", "s", {"mission_id": "future"}, "pi"))
    ingest.ingest(event("mission", "future", {}, "herdr"))
    ingest.ingest(event("mission", "future", {}, "pi"))
    assert repo.get_mission("future")["sessions"] == []
    assert repo.get_mission("pi:future")["sessions"] == ["s"]
