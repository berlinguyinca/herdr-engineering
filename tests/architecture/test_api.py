import copy
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import REPO_ID, git, write  # noqa: E402

from herdr_engineering.architecture import analyze, identity  # noqa: E402
from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402


def start(**kw):
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, **kw)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr, repo, f"http://127.0.0.1:{svr.port}"


def call(base, path, body=None, key=None, method=None, headers=None):
    h = {"Content-Type": "application/json", **({"X-API-Key": key} if key else {}), **(headers or {})}
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method or ("POST" if body is not None else "GET"), headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def api():
    svr, repo, base = start(architecture_enabled=True)
    yield base, repo, svr
    svr.shutdown()


@pytest.fixture
def snaps(repo):
    a = analyze.analyze_commit(str(repo), "HEAD", "h1", REPO_ID)
    write(repo, {"app/util.py": "import json\n\ndef helper():\n    return 2\n\ndef added(): pass\n", "app/gone.py": "x=1\n"})
    git(repo, "rm", "-q", "app/core.py")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "change")
    b = analyze.analyze_commit(str(repo), "HEAD", "h1", REPO_ID)
    return a, b


def test_flag_off_by_default_hides_everything():
    svr, repo, base = start()
    try:
        assert call(base, "/api/v1/architecture/snapshots")[0] == 404
        assert call(base, "/api/v1/architecture/snapshots", {})[0] == 404
        assert "architecture_v1" not in call(base, "/api/v1/health")[1]["capabilities"]
    finally:
        svr.shutdown()


def test_capability_advertised_when_enabled(api):
    base, _, _ = api
    assert "architecture_v1" in call(base, "/api/v1/health")[1]["capabilities"]


def test_publish_query_roundtrip_and_idempotence(api, snaps):
    base, repo, _ = api
    a, _b = snaps
    st, out = call(base, "/api/v1/architecture/snapshots", a)
    assert st == 201 and not out["duplicate"]
    assert call(base, "/api/v1/architecture/snapshots", a)[0] == 200          # idempotent
    sid = a["snapshot"]["snapshot_id"]
    listed = call(base, f"/api/v1/architecture/snapshots?repository_id={REPO_ID}")[1]
    assert [s["snapshot_id"] for s in listed["snapshots"]] == [sid]
    kids = call(base, f"/api/v1/architecture/snapshots/{sid}/children")[1]
    assert [n["node_id"] for n in kids["nodes"]] == ["package:app"] and kids["parent"]["node_id"] == "repo:root"
    app = call(base, f"/api/v1/architecture/snapshots/{sid}/children?parent=package:app")[1]
    agg = {(e["from_id"], e["to_id"]) for e in app["edges"]}
    assert ("module:app", "module:app.core") in agg and ("module:app.core", "module:app.util") in agg
    hit = call(base, f"/api/v1/architecture/snapshots/{sid}/nodes?q=helper")[1]
    assert hit["nodes"][0]["relative_path"] == "app/util.py" and hit["nodes"][0]["line_start"] == 3
    ex = call(base, f"/api/v1/architecture/snapshots/{sid}/nodes/module:app.core/edges?direction=out")[1]
    assert {e["to_id"] for e in ex["edges"]} == {"module:app.util"} and all(e["source_span"] for e in ex["edges"])
    assert call(base, f"/api/v1/architecture/snapshots/{sid}/nodes/nope")[0] == 404


def test_published_snapshot_event_has_no_source_or_graph_blob(api, snaps):
    base, repo, _ = api
    call(base, "/api/v1/architecture/snapshots", snaps[0])
    sid = snaps[0]["snapshot"]["snapshot_id"]
    ev = repo.stream("architecture_snapshot", sid)[0]
    assert ev["event_type"] == "architecture.snapshot.published"
    assert '"qualified_name"' not in json.dumps(ev) and "def helper" not in json.dumps(ev)
    assert ev["payload"]["snapshot"]["artifact_ref"]["revision"] == snaps[0]["snapshot"]["commit_sha"]


def test_tampering_is_rejected(api, snaps):
    base, _, _ = api
    bad = copy.deepcopy(snaps[0]); bad["graph"]["nodes"][0]["qualified_name"] = "evil"
    assert call(base, "/api/v1/architecture/snapshots", bad)[0] == 400                   # graph_hash mismatch
    bad = copy.deepcopy(snaps[0]); bad["snapshot"]["commit_sha"] = "b" * 40
    assert call(base, "/api/v1/architecture/snapshots", bad)[0] == 400                   # snapshot_id mismatch
    for mutate in (lambda g: g["edges"].append({**g["edges"][0], "to_id": "ghost"}),
                   lambda g: g["nodes"][1].update(relative_path="../../etc/passwd"),
                   lambda g: g["nodes"].append(dict(g["nodes"][0])),
                   lambda g: g["edges"][0].update(inferred="no")):
        bad = copy.deepcopy(snaps[0]); mutate(bad["graph"])
        bad["snapshot"]["graph_hash"] = identity.graph_hash(bad["graph"])                   # attacker re-hashes
        assert call(base, "/api/v1/architecture/snapshots", bad)[0] == 400


def test_snapshot_is_immutable(api, snaps):
    base, repo, svr = api
    a = snaps[0]
    call(base, "/api/v1/architecture/snapshots", a)
    other = copy.deepcopy(a); other["graph"]["diagnostics"].append({"code": "x"})
    other["snapshot"]["graph_hash"] = identity.graph_hash(other["graph"])
    assert call(base, "/api/v1/architecture/snapshots", other)[0] == 409


def test_pagination_is_bounded_with_cursor(api, snaps):
    base, _, _ = api
    call(base, "/api/v1/architecture/snapshots", snaps[0])
    sid = snaps[0]["snapshot"]["snapshot_id"]
    p1 = call(base, f"/api/v1/architecture/snapshots/{sid}/nodes?limit=5")[1]
    assert len(p1["nodes"]) == 5 and p1["next_cursor"]
    p2 = call(base, f"/api/v1/architecture/snapshots/{sid}/nodes?limit=5&cursor={p1['next_cursor']}")[1]
    assert not {n["node_id"] for n in p1["nodes"]} & {n["node_id"] for n in p2["nodes"]}
    assert len(call(base, f"/api/v1/architecture/snapshots/{sid}/nodes?limit=99999")[1]["nodes"]) <= 500


def test_diff_reports_added_removed_modified_nodes_and_edges(api, snaps):
    base, _, _ = api
    a, b = snaps
    call(base, "/api/v1/architecture/snapshots", a); call(base, "/api/v1/architecture/snapshots", b)
    d = call(base, f"/api/v1/architecture/diff?base={a['snapshot']['snapshot_id']}&head={b['snapshot']['snapshot_id']}")[1]
    items = {(i["change"], i["type"], i.get("id") or (i["from_id"], i["to_id"])) for i in d["items"]}
    assert ("added", "node", "function:app.util.added") in items
    assert ("removed", "node", "module:app.core") in items
    assert ("modified", "node", "function:app.util.helper") in items
    assert ("removed", "edge", ("module:app.core", "module:app.util")) in items
    assert d["summary"]["nodes_added"] >= 2 and any("rename" in w for w in d["warnings"])
    assert call(base, "/api/v1/architecture/diff?base=x")[0] == 400


def test_diff_refuses_cross_repository(api, repo):
    base, _, _ = api
    a = analyze.analyze_commit(str(repo), "HEAD", "h1", REPO_ID)
    b = analyze.analyze_commit(str(repo), "HEAD", "h1", "github.com/example/other")
    for x in (a, b):
        call(base, "/api/v1/architecture/snapshots", x)
    assert call(base, f"/api/v1/architecture/diff?base={a['snapshot']['snapshot_id']}&head={b['snapshot']['snapshot_id']}")[0] == 400


def test_generic_ingest_cannot_forge_architecture_streams(api):
    base, _, _ = api
    st, body = call(base, "/api/v1/ingest", {"entity_type": "architecture_snapshot", "entity_id": "arch_x",
                                             "event_type": "architecture.snapshot.published", "payload": {}})
    assert st == 400 and "reserved" in body["error"]


def test_analysis_lifecycle_events(api):
    base, repo, _ = api
    ident = {"analysis_id": "an-1", "repository_id": REPO_ID, "commit_sha": "a" * 40, "host_id": "h1"}
    for ev, extra in (("requested", {}), ("started", {}), ("progress", {"progress": 0.5}), ("completed", {})):
        assert call(base, "/api/v1/architecture/analyses", {**ident, "event": ev, **extra})[0] == 201
    a = call(base, "/api/v1/architecture/analyses")[1]["analyses"][0]
    assert a["state"] == "completed" and a["analysis_id"] == "an-1"
    assert call(base, "/api/v1/architecture/analyses", {**ident, "event": "bogus"})[0] == 400
    assert call(base, "/api/v1/architecture/analyses", {**ident, "event": "progress", "progress": 7})[0] == 400


def test_role_enforcement_and_origin_guard(snaps):
    svr, repo, base = start(api_keys={"v": "viewer", "o": "operator", "a": "agent", "i": "ingest"},
                            architecture_enabled=True)
    try:
        a = snaps[0]
        assert call(base, "/api/v1/architecture/snapshots")[0] == 401                       # anonymous
        assert call(base, "/api/v1/architecture/snapshots", a, key="v")[0] == 403          # viewer cannot publish
        assert call(base, "/api/v1/architecture/snapshots", a, key="i")[0] == 403          # ingest role cannot
        assert call(base, "/api/v1/architecture/snapshots", a, key="o",
                    headers={"Origin": "http://evil.example"})[0] == 403                    # cross-origin refused
        assert call(base, "/api/v1/architecture/snapshots", a, key="a")[0] == 201          # host agent can
        assert call(base, "/api/v1/architecture/snapshots", key="v")[0] == 200             # viewer reads
        assert call(base, "/api/v1/architecture/snapshots", a, key="a", headers={"Content-Type": "text/plain"})[0] == 415
    finally:
        svr.shutdown()


def test_fleet_config_flag_is_strict(tmp_path):
    class Cfg:
        def __init__(self, section): self._s = section
        def current(self): return self
        def section(self, name): return self._s
    assert ControlPlaneServer._architecture_enabled(Cfg({"architecture": {"enabled": True}}), None) is True
    assert ControlPlaneServer._architecture_enabled(Cfg({}), None) is False
    with pytest.raises(ValueError):
        ControlPlaneServer._architecture_enabled(Cfg({"architecture": {"enabled": True, "typo": 1}}), None)
    with pytest.raises(ValueError):
        ControlPlaneServer._architecture_enabled(Cfg({"architecture": {"enabled": "yes"}}), None)
