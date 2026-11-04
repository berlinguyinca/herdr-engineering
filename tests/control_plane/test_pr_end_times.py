"""Pull request end times as they travel: ingest envelope -> repo record -> durable event -> replay after a restart.

A time the provider reported stays "reported"; a time this server stamped when it first saw the PR merged or closed stays
"observed", also after a restart (replay used to mark every replayed time "reported").
"""
import json

from herdr_engineering.control_plane.bridge import load_into
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


class FakeSource:
    def __init__(self, events):
        self._e = list(events)

    def missions(self):
        return []

    def sessions(self):
        return []

    def services(self):
        return []

    def hosts(self):
        return []

    def events(self):
        return self._e


def _pull(svc, status, **payload):
    return svc.ingest({"entity_type": "pull_request", "entity_id": "pr-end", "event_type": "pull_request.updated",
                       "payload": {"mission_id": "m1", "repository": "acme/w", "number": 9, "status": status, **payload}})


def _events(repo):
    out = []
    for e in repo.stream("pull_request", "pr-end"):
        payload = e["payload"] if isinstance(e["payload"], dict) else json.loads(e["payload"])
        out.append((e["event_type"], payload))
    return out


def test_ingest_keeps_the_times_the_provider_reported():
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    _pull(svc, "open")
    _pull(svc, "merged", merged_at="2026-10-01T10:00:00Z", closed_at="2026-10-01T10:00:00Z")
    pr = repo.get_pull_request("pr-end")
    assert pr["merged_at"] == "2026-10-01T10:00:00Z" and pr["closed_at"] == "2026-10-01T10:00:00Z"
    assert pr["ended_source"] == "reported"


def test_ingest_without_usable_provider_times_is_observed():
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    _pull(svc, "merged", merged_at="yesterday", closed_at={"x": 1})
    pr = repo.get_pull_request("pr-end")
    assert pr["merged_at"] and pr["merged_at"] != "yesterday" and pr["ended_source"] == "observed"


def test_ingest_times_survive_the_delivery_redactor_into_the_event():
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    _pull(svc, "closed", closed_at="2026-10-02T08:30:00+00:00")
    assert any(p.get("closed_at") == "2026-10-02T08:30:00+00:00" and p.get("ended_source") == "reported" for _, p in _events(repo))


def _pull_event(seq, kind, **payload):
    ts = "2026-01-01T00:00:00+00:00"
    return dict(event_id=f"p{seq}", sequence=seq, event_type=kind, entity_type="pull_request", entity_id="pr9",
                mission_id="pi:M1", payload=json.dumps({"mission_id": "pi:M1", "repository": "o/r", "number": 9, **payload}),
                source_timestamp=ts, ingest_timestamp=ts, schema_version=1)


def test_replay_keeps_an_observed_end_time_observed():
    events = [_pull_event(0, "PullRequestOpened", status="open"),
              _pull_event(1, "PullRequestUpdated", status="merged", merged_at="2026-10-01T10:00:00+00:00", closed_at=None, ended_source="observed")]
    repo = ControlPlaneRepo()
    load_into(repo, FakeSource(events))
    pr = repo.get_pull_request("pr9")
    assert pr["merged_at"] == "2026-10-01T10:00:00+00:00" and pr["ended_source"] == "observed"


def test_replay_keeps_a_reported_end_time_reported():
    events = [_pull_event(0, "PullRequestOpened", status="open"),
              _pull_event(1, "PullRequestUpdated", status="closed", merged_at=None, closed_at="2026-10-02T08:00:00+00:00", ended_source="reported")]
    repo = ControlPlaneRepo()
    load_into(repo, FakeSource(events))
    pr = repo.get_pull_request("pr9")
    assert pr["closed_at"] == "2026-10-02T08:00:00+00:00" and pr["ended_source"] == "reported"


def test_replay_of_a_log_written_before_end_times_existed_still_loads():
    events = [_pull_event(0, "PullRequestOpened", status="open"), _pull_event(1, "PullRequestUpdated", status="merged")]
    repo = ControlPlaneRepo()
    load_into(repo, FakeSource(events))
    pr = repo.get_pull_request("pr9")
    assert pr["status"] == "merged" and pr["merged_at"] and pr["ended_source"] == "observed"


def test_replay_of_an_event_with_a_time_but_no_source_is_reported_not_guessed_observed():
    events = [_pull_event(0, "PullRequestOpened", status="open"),
              _pull_event(1, "PullRequestUpdated", status="merged", merged_at="2026-10-01T10:00:00+00:00")]
    repo = ControlPlaneRepo()
    load_into(repo, FakeSource(events))
    assert repo.get_pull_request("pr9")["ended_source"] == "reported"
