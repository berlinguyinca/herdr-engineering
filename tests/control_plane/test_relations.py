# ruff: noqa: F811  (the public-listener fixture is imported by name)
"""Cross-linking relations (design 2026-10-24, step 2)."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane import relations
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab_public import stack  # noqa: F401


def _world():
    repo = ControlPlaneRepo()
    repo.upsert_host("beast", "beast", "100.1.1.1")
    repo.upsert_mission("m1", title="Fix parser", stage="active")
    repo.upsert_session("s1", host_id="beast", mission_id="m1", agent_role="pi", repository_id="org/app", branch="fix/parser")
    repo.upsert_session("s2", host_id="beast", mission_id="m1", agent_role="pi", parent_session_id="s1")
    repo.upsert_session("s9", host_id="beast", agent_role="pi")
    repo.upsert_review("r1", mission_id="m1", session_id="s1", reviewer="codex", status="done", verdict="approved")
    repo.upsert_pull_request("pr1", mission_id="m1", repository="org/app", number=214, status="open", branch="fix/parser")
    repo.observe_ci_pipeline({"repository": "org/app", "provider_repo_id": 7, "run_number": 12, "branch": "fix/parser", "state": "success"})
    repo.observe_ci_pipeline({"repository": "org/app", "provider_repo_id": 7, "run_number": 13, "branch": "other", "state": "failure"})
    svc = repo.register_service("web", "beast", 8080, session_id="s1")
    return repo, svc


def _ids(answer, group):
    return {i["id"] for i in answer["relations"].get(group, [])}


def test_mission_links_agents_host_ci_review_pr():
    repo, _ = _world()
    a = relations.related(repo, "mission", "m1")
    assert _ids(a, "agents") == {"s1", "s2"} and _ids(a, "host") == {"beast"}
    assert _ids(a, "ci") == {"7:12"}          # the other branch's run is not this mission's
    assert _ids(a, "review") == {"r1"} and _ids(a, "pr") == {"pr1"}
    assert a["relations"]["llm"] == {"requests": 0, "tokens_out": 0, "problems": 0}
    assert a["partial"] == []


def test_session_links_mission_host_parent_child_service_ci_review():
    repo, svc = _world()
    a = relations.related(repo, "session", "s1")
    assert _ids(a, "mission") == {"m1"} and _ids(a, "host") == {"beast"}
    assert {(i["id"], i["relation"]) for i in a["relations"]["agents"]} == {("s2", "child")}
    assert _ids(a, "service") == {svc} and _ids(a, "ci") == {"7:12"} and _ids(a, "review") == {"r1"}
    assert {(i["id"], i["relation"]) for i in relations.related(repo, "session", "s2")["relations"]["agents"]} == {("s1", "parent")}


def test_host_service_ci_review_pr_project():
    repo, svc = _world()
    assert _ids(relations.related(repo, "host", "beast"), "agents") == {"s1", "s2", "s9"}
    assert _ids(relations.related(repo, "host", "beast"), "service") == {svc}
    sv = relations.related(repo, "service", svc)
    assert _ids(sv, "host") == {"beast"} and _ids(sv, "agents") == {"s1"}
    ci = relations.related(repo, "ci", "7:12")
    assert _ids(ci, "agents") == {"s1"} and _ids(ci, "mission") == {"m1"} and _ids(ci, "pr") == {"pr1"} and _ids(ci, "review") == {"r1"}
    rv = relations.related(repo, "review", "r1")
    assert _ids(rv, "agents") == {"s1"} and _ids(rv, "mission") == {"m1"} and _ids(rv, "pr") == {"pr1"} and _ids(rv, "ci") == {"7:12"}
    pr = relations.related(repo, "pr", "pr1")
    assert _ids(pr, "mission") == {"m1"} and _ids(pr, "review") == {"r1"} and _ids(pr, "ci") == {"7:12"}
    pj = relations.related(repo, "project", "org/app")
    assert _ids(pj, "agents") == {"s1"} and _ids(pj, "ci") == {"7:12", "7:13"} and _ids(pj, "pr") == {"pr1"} and _ids(pj, "mission") == {"m1"}


def test_unknown_entity_and_bad_input():
    repo, _ = _world()
    for kind, eid in [("mission", "nope"), ("session", "nope"), ("host", "nope"), ("ci", "7:99"), ("project", "x/y")]:
        assert relations.related(repo, kind, eid) is None
    with pytest.raises(ValueError):
        relations.related(repo, "bogus", "a")
    with pytest.raises(ValueError):
        relations.related(repo, "mission", "")
    with pytest.raises(ValueError):
        relations.related(repo, "mission", "x" * 301)


def test_lists_are_capped_with_a_more_count():
    repo = ControlPlaneRepo()
    repo.upsert_host("h", "h", "100.1.1.2")
    for i in range(27):
        repo.upsert_session(f"s{i}", host_id="h", agent_role="pi")
    a = relations.related(repo, "host", "h")
    assert len(a["relations"]["agents"]) == relations.CAP and a["more"] == {"agents": 7}


def test_a_failing_source_is_named_partial_not_empty():
    repo, _ = _world()
    repo.ci_reports = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    a = relations.related(repo, "mission", "m1")
    assert a["partial"] == ["ci"] and "ci" not in a["relations"] and _ids(a, "agents") == {"s1", "s2"}


def test_labels_are_bounded_and_unprintable_stripped():
    repo = ControlPlaneRepo()
    repo.upsert_mission("m", title="x" * 500 + "\x00", stage="active")
    repo.upsert_session("s", host_id="h", mission_id="m", agent_role="pi")
    item = relations.related(repo, "session", "s")["relations"]["mission"][0]
    assert len(item["label"]) <= 120 and "\x00" not in item["label"]


def _serve(repo, **kw):
    svr = ControlPlaneServer(repo, host="127.0.0.1", port=0, **kw)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr


def _get(svr, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{svr.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_route_statuses():
    repo, _ = _world()
    svr = _serve(repo, allow_open=True)
    try:
        st, body = _get(svr, "/api/v1/related?kind=session&id=s1")
        assert st == 200 and body["kind"] == "session" and _ids(body, "mission") == {"m1"}
        assert _get(svr, "/api/v1/related?kind=bogus&id=s1")[0] == 400
        assert _get(svr, "/api/v1/related?kind=session")[0] == 400
        assert _get(svr, "/api/v1/related?kind=session&id=zzz")[0] == 404
        assert _get(svr, "/api/v1/related?kind=session&id=" + "a" * 400)[0] == 400
    finally:
        svr.shutdown()


@pytest.mark.parametrize("path", ["/api/v1/related?kind=session&id=s1", "/entity-links.js"])
def test_related_does_not_exist_on_the_public_listener(stack, path):
    status, _ = stack.pub.call("GET", path)
    assert status == 404
