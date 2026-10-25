"""Review fixes: deep snapshot validation before any write, cycle guards, a bounded graph cache, agent host
binding, request expiry + operator cancel, room-id redaction, the path check, and a resilient serve loop."""
import copy
import json
import os
import sys
import time
import urllib.error
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import REPO_ID, git, write  # noqa: E402
from test_api import call, start  # noqa: E402

from herdr_engineering.architecture import analyze, command, identity  # noqa: E402
from herdr_engineering.architecture.service import (ArchitectureError, ArchitectureService, _Loaded,  # noqa: E402
                                                    _REL)
from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

SNAPS = "/api/v1/architecture/snapshots"


@pytest.fixture
def api():
    svr, repo, base = start(architecture_enabled=True)
    yield base, repo, svr
    svr.shutdown()


@pytest.fixture
def snap(repo):
    return analyze.analyze_commit(str(repo), "HEAD", "h1", REPO_ID)


def rehashed(s, mutate):
    bad = copy.deepcopy(s)
    mutate(bad["graph"])
    bad["snapshot"]["graph_hash"] = identity.graph_hash(bad["graph"])     # get past the hash check
    return bad


def node(g, nid):
    return next(n for n in g["nodes"] if n["node_id"] == nid)


MALFORMED = {
    "node without parent_id": (lambda g: node(g, "module:app.core").pop("parent_id"), "are required"),
    "node without qualified_name": (lambda g: node(g, "module:app.core").pop("qualified_name"), "are required"),
    "unknown parent": (lambda g: node(g, "module:app.core").update(parent_id="ghost"), "unknown node"),
    "cycle a->b->a": (lambda g: (node(g, "package:app").update(parent_id="module:app.core")), "cycle"),
    "two roots": (lambda g: node(g, "package:app").update(parent_id=None), "exactly one root"),
    "edge without edge_id": (lambda g: g["edges"][0].pop("edge_id"), "are required"),
    "edge without evidence_ref": (lambda g: g["edges"][0].pop("evidence_ref"), "are required"),
    "edge with a bad line_start": (lambda g: g["edges"][-1]["source_span"].update(line_start="7"), "source span"),
    "edge span path escapes": (lambda g: g["edges"][-1]["source_span"].update(path="../x"), "source span"),
    "node path with a newline": (lambda g: node(g, "module:app.core").update(relative_path="x\n../y"), "escapes"),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_malformed_snapshot_is_rejected_and_nothing_is_stored(api, snap, case):
    base, repo, svr = api
    mutate, message = MALFORMED[case]
    bad = rehashed(snap, mutate)
    artifacts = len(repo._artifacts)
    st, body = call(base, SNAPS, bad)
    assert st == 400 and message in body["error"], body
    sid = snap["snapshot"]["snapshot_id"]
    assert repo.stream("architecture_snapshot", sid) == []
    assert sid not in svr.architecture._meta and sid not in svr.architecture._graphs
    assert len(repo._artifacts) == artifacts                               # no orphan artifact either
    # the corrected snapshot publishes normally afterwards
    st, out = call(base, SNAPS, snap)
    assert st == 201 and out["duplicate"] is False


def test_rel_check_rejects_control_characters_and_dotdot_across_newlines():
    assert _REL.fullmatch("app/core.py")
    for bad in ("x\n../y", "../y", "/abs", "a/../b", "a\\b", "a\x00b", "a\rb", "a\x7fb"):
        assert not _REL.fullmatch(bad), repr(bad)


def test_ancestors_and_aggregate_are_bounded_on_a_cyclic_legacy_graph():
    g = _Loaded({"nodes": [{"node_id": "a", "parent_id": "b", "kind": "module"},
                           {"node_id": "b", "parent_id": "a", "kind": "module"}], "edges": []})
    with pytest.raises(ArchitectureError):
        list(g.ancestors("a"))
    svc = ArchitectureService(ControlPlaneRepo(), enabled=True)
    assert svc._aggregate(g, ["a"]) == []                 # returns instead of looping over the cycle


def test_a_cyclic_graph_stored_before_validation_is_refused_on_load(api, snap):
    base, repo, svr = api
    assert call(base, SNAPS, snap)[0] == 201
    sid = snap["snapshot"]["snapshot_id"]
    arch = svr.architecture
    bad = rehashed(snap, MALFORMED["cycle a->b->a"][0])["graph"]
    data = identity.canonical_bytes(bad)
    aid = repo.register_artifact(original_filename="x.json", mime_type="application/json", data=data)
    art = repo.get_artifact(aid)
    arch._meta[sid] = {**arch._meta[sid], "artifact_ref": {**arch._meta[sid]["artifact_ref"], "artifact_id": aid,
                                                           "sha256": art["sha256"]}}
    arch._graphs.clear()
    st, body = call(base, f"{SNAPS}/{sid}/children")
    assert st == 503 and "malformed" in body["error"]


def test_graph_cache_is_an_lru_with_count_and_byte_budgets(repo):
    crepo = ControlPlaneRepo()
    arch = ArchitectureService(crepo, enabled=True, cache_graphs=2)
    sids = []
    for i in range(3):
        write(repo, {f"app/m{i}.py": f"x = {i}\n"})
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", f"c{i}")
        s = analyze.analyze_commit(str(repo), "HEAD", "h1", REPO_ID)
        arch.publish(s)
        sids.append(s["snapshot"]["snapshot_id"])
    assert list(arch._graphs) == sids[1:]                   # the oldest was evicted
    arch._load(sids[1])                                      # touch: sids[2] is now least recent
    reads = []
    real = crepo.get_artifact_data
    crepo.get_artifact_data = lambda aid: reads.append(aid) or real(aid)
    assert "function:app.core.run" in arch._load(sids[0]).nodes   # reloaded from its artifact on demand
    assert len(reads) == 1 and list(arch._graphs) == [sids[1], sids[0]]
    tiny = ArchitectureService(crepo, enabled=True, cache_bytes=1)
    for sid in sids:
        tiny._load(sid)
    assert list(tiny._graphs) == [sids[2]]                  # over the byte budget: keep only the one returned


def test_cache_and_expiry_are_yaml_configurable_and_strict():
    opts = ControlPlaneServer._architecture_options(None, {"cache": {"max_graphs": 3, "max_bytes": 2 ** 20},
                                                           "request_ttl_seconds": 120})
    assert (opts["cache_graphs"], opts["cache_bytes"], opts["request_ttl"]) == (3, 2 ** 20, 120)
    defaults = ControlPlaneServer._architecture_options(None, None)
    assert defaults["request_ttl"] == 3600 and defaults["cache_graphs"] >= 1
    for bad in ({"cache": {"max_graphs": 0}}, {"cache": {"size": 1}}, {"request_ttl_seconds": "1h"},
                {"agent_hosts": {"k": []}}, {"typo": 1}):
        with pytest.raises(ValueError, match="control_plane.architecture"):
            ControlPlaneServer._architecture_options(None, bad)


def test_unbound_agent_keys_are_trusted_for_host_id_bound_keys_are_not(snap):
    """Documented: API keys carry a role, not a host. An agent key listed in control_plane.architecture.agent_hosts
    may act only for those hosts; an unlisted agent key may name any host_id."""
    svr, repo, base = start(api_keys={"o": "operator", "free": "agent", "bound": "agent"}, architecture_enabled=True,
                            architecture_options={"agent_hosts": {"bound": ["h2"]}})
    try:
        req = {"repository_id": REPO_ID, "rev": "main"}
        call(base, "/api/v1/architecture/analyses/request", {**req, "host_id": "h1"}, key="o")
        call(base, "/api/v1/architecture/analyses/request", {**req, "host_id": "h2"}, key="o")
        assert call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1"}, key="bound")[0] == 403
        assert call(base, SNAPS, snap, key="bound")[0] == 403                       # snapshot names h1
        st, got = call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1"}, key="free")
        assert st == 200 and got["job"]["host_id"] == "h1"                           # trusted: any host
        aid = got["job"]["analysis_id"]
        report = {"analysis_id": aid, "event": "progress", "host_id": "h1"}
        assert call(base, "/api/v1/architecture/analyses", report, key="bound")[0] == 403
        assert call(base, "/api/v1/architecture/analyses", {**report, "host_id": "h2"}, key="free")[0] == 409
        assert call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h2"}, key="bound")[1]["job"]
        assert call(base, SNAPS, snap, key="free")[0] == 201
    finally:
        svr.shutdown()
    with pytest.raises(ValueError, match="agent role"):
        ControlPlaneServer(ControlPlaneRepo(), api_keys={"o": "operator"},
                           architecture_options={"agent_hosts": {"o": ["h1"]}})


def test_require_host_binding_refuses_unbound_agent_keys(snap):
    """control_plane.architecture.require_host_binding: true (recommended in production) refuses every
    architecture POST from an agent key with no agent_hosts entry; a bound key still acts only for its hosts."""
    svr, repo, base = start(api_keys={"o": "operator", "free": "agent", "bound": "agent"}, architecture_enabled=True,
                            architecture_options={"agent_hosts": {"bound": ["h1"]}, "require_host_binding": True})
    try:
        assert svr.architecture_require_host_binding is True
        call(base, "/api/v1/architecture/analyses/request", {"repository_id": REPO_ID, "rev": "main",
                                                              "host_id": "h1"}, key="o")
        unbound = "this API key is not bound to any host (control_plane.architecture.agent_hosts)"
        for path, body in (("/api/v1/architecture/analyses/claim", {"host_id": "h1"}), (SNAPS, snap),
                           ("/api/v1/architecture/analyses", {"analysis_id": "an-x", "event": "progress",
                                                              "host_id": "h1"})):
            st, out = call(base, path, body, key="free")
            assert (st, out) == (403, {"error": unbound})
        assert call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h9"}, key="bound")[0] == 403
        st, got = call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1"}, key="bound")
        assert st == 200 and got["job"]["host_id"] == "h1"
        assert call(base, SNAPS, snap, key="bound")[0] == 201                          # snapshot names h1
    finally:
        svr.shutdown()
    assert ControlPlaneServer._architecture_options(None, None)["require_host_binding"] is False   # default off
    with pytest.raises(ValueError, match="require_host_binding must be a boolean"):
        ControlPlaneServer._architecture_options(None, {"require_host_binding": "yes"})


def test_requested_jobs_expire_and_operators_can_cancel():
    svr, repo, base = start(api_keys={"o": "operator", "a": "agent", "v": "viewer"}, architecture_enabled=True,
                            architecture_options={"request_ttl_seconds": 60})
    try:
        body = {"repository_id": REPO_ID, "host_id": "h1", "rev": "main"}
        aid = call(base, "/api/v1/architecture/analyses/request", body, key="o")[1]["analysis_id"]
        job = svr.architecture._jobs[aid]
        assert job["expires_at"] == pytest.approx(time.time() + 60, abs=5)
        job["expires_at"] = time.time() - 1
        assert call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1"}, key="a")[1]["job"] is None
        assert call(base, f"/api/v1/architecture/analyses/{aid}", key="v")[1]["analysis"]["state"] == "expired"
        st, again = call(base, "/api/v1/architecture/analyses/request", body, key="o")
        assert st == 201 and again["analysis_id"] != aid                             # an expired one never dedups
        cancel = f"/api/v1/architecture/analyses/{again['analysis_id']}/cancel"
        assert call(base, cancel, {}, key="a")[0] == 403
        assert call(base, cancel, {}, key="v")[0] == 403
        st, out = call(base, cancel, {}, key="o")
        assert st == 200 and out["analysis"]["state"] == "cancelled"
        assert call(base, cancel, {}, key="o")[0] == 409
        assert call(base, "/api/v1/architecture/analyses/an-nope/cancel", {}, key="o")[0] == 404
    finally:
        svr.shutdown()
    fresh = ArchitectureService(repo, enabled=True)                                # replayed from events
    assert fresh._jobs[aid]["state"] == "expired" and fresh._jobs[again["analysis_id"]]["state"] == "cancelled"


def test_legacy_requested_job_without_expiry_uses_created_at():
    svc = ArchitectureService(ControlPlaneRepo(), enabled=True, request_ttl=60)
    assert svc._expires_at({"created_at": "2020-01-01T00:00:00Z"}) < time.time()
    assert not svc._open({"state": "requested", "created_at": "2020-01-01T00:00:00Z"})


def test_node_rooms_reveals_room_ids_to_operators_only(snap):
    svr, repo, base = start(api_keys={"o": "operator", "v": "viewer"}, architecture_enabled=True)
    try:
        assert call(base, SNAPS, snap, key="o")[0] == 201
        sid = snap["snapshot"]["snapshot_id"]
        svr.architecture._links["room_secret"].append({"link_id": "l", "snapshot_id": sid, "node_id": "module:app.core",
                                                       "note": "", "author": "x", "created_at": "t"})
        path = f"{SNAPS}/{sid}/nodes/module:app.core/rooms"
        assert call(base, path, key="o")[1] == {"rooms": ["room_secret"], "count": 1, "redacted": False}
        st, got = call(base, path, key="v")
        assert st == 200 and got == {"rooms": [], "count": 1, "redacted": True}
        assert "room_secret" not in json.dumps(got)
        assert call(base, f"{SNAPS}/{sid}/nodes/ghost/rooms", key="v")[0] == 404
    finally:
        svr.shutdown()


# ---------------------------------------------------------------- serve resilience (fake transport)
def serve_args(tmp_path, **kw):
    key = tmp_path / "key"
    key.write_text("k")
    return SimpleNamespace(url="http://cp.invalid", api_key_file=str(key), host_id="h1", checkout=[], poll=1.0,
                           once=False, timeout=5.0, cache_dir=str(tmp_path / "c"), **kw)


class Stop(Exception):
    pass


def test_serve_backs_off_on_unreachable_refused_and_garbage_answers(tmp_path, monkeypatch):
    answers = [urllib.error.URLError("connection refused"), command.ControlPlaneRefused("control plane refused (503)"),
               json.JSONDecodeError("bad", "x", 0), ["not", "an", "object"], {"job": None}]
    calls, sleeps = [], []

    def fake_post(url, body, key):
        calls.append(url)
        got = answers.pop(0)
        if isinstance(got, Exception):
            raise got
        return got

    def fake_sleep(s):
        sleeps.append(s)
        if not answers:
            raise Stop
    monkeypatch.setattr(command, "_post", fake_post)
    monkeypatch.setattr(command.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        command.cmd_serve(serve_args(tmp_path))
    assert len(calls) == 5 and all(u.endswith("/analyses/claim") for u in calls)
    assert sleeps[:4] == [2.0, 4.0, 8.0, 16.0] and sleeps[4] == 1.0               # backoff, reset after success


def test_backoff_is_capped(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(command, "_post", lambda *a: (_ for _ in ()).throw(urllib.error.URLError("down")))
    monkeypatch.setattr(command.time, "sleep", lambda s: sleeps.append(s) or (len(sleeps) >= 12 and (_ for _ in ()).throw(Stop)))
    with pytest.raises(Stop):
        command.cmd_serve(serve_args(tmp_path))
    assert max(sleeps) == command.MAX_BACKOFF


def test_a_refused_failed_report_never_ends_the_worker(tmp_path, monkeypatch):
    """The job's 'failed' post is refused (e.g. 409 after an operator cancel): serve logs it and keeps claiming."""
    posts = []
    job = {"analysis_id": "an-1", "repository_id": REPO_ID, "host_id": "h1", "rev": "main"}

    def fake_post(url, body, key):
        posts.append((url.rsplit("/api/v1/architecture/", 1)[1], body.get("event")))
        if url.endswith("/claim"):
            return {"job": job} if len(posts) == 1 else {"job": None}
        raise command.ControlPlaneRefused("control plane refused (409): analysis already finished")
    monkeypatch.setattr(command, "_post", fake_post)
    monkeypatch.setattr(command.time, "sleep", lambda s: (_ for _ in ()).throw(Stop))
    with pytest.raises(Stop):
        command.cmd_serve(serve_args(tmp_path))                 # no SystemExit
    assert posts[:3] == [("analyses/claim", None), ("analyses", "failed"), ("analyses/claim", None)]


def test_serve_once_reports_a_transport_failure_as_exit_1(tmp_path, monkeypatch):
    monkeypatch.setattr(command, "_post", lambda *a: (_ for _ in ()).throw(urllib.error.URLError("down")))
    monkeypatch.setattr(command.time, "sleep", lambda s: None)
    args = serve_args(tmp_path)
    args.once = True
    assert command.cmd_serve(args) == 1
