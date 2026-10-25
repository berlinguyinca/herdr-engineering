import http.server
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import git, write  # noqa: E402
from test_api import call, start  # noqa: E402

from herdr_engineering.architecture import analyze, crossrepo, explain  # noqa: E402
from herdr_engineering.architecture.service import ArchitectureError, ArchitectureService  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

LIB, APP, OTHER = "github.com/example/lib", "github.com/example/app", "github.com/example/other"


def make(tmp_path, name, files):
    root = tmp_path / name
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, files)
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "x")
    return root


@pytest.fixture
def trio(tmp_path):
    lib = make(tmp_path, "lib", {"mylib/__init__.py": "", "mylib/core.py": "def f(): pass\n"})
    app = make(tmp_path, "app", {"app/main.py": "import os\nimport mylib.core\nfrom mylib import core\nimport requests\nimport json\n",
                                  "app/b.py": "import vendored.thing\n",
                                  "app/c.py": "from app import main\n"})
    other = make(tmp_path, "other", {"mylib/__init__.py": "", "vendored/thing.py": "x = 1\n"})   # also ships `mylib`
    snaps = {n: analyze.analyze_commit(str(r), "HEAD", "h1", rid) for n, r, rid in
             (("lib", lib, LIB), ("app", app, APP), ("other", other, OTHER))}
    return snaps


@pytest.fixture
def svc_api(trio):
    svr, crepo, base = start(architecture_enabled=True)
    for s in trio.values():
        assert call(base, "/api/v1/architecture/snapshots", s)[0] == 201
    ids = {k: v["snapshot"]["snapshot_id"] for k, v in trio.items()}
    yield base, ids, svr
    svr.shutdown()


def xr(base, ids, names, maps=()):
    q = "&".join([f"snapshot={ids[n]}" for n in names] + [f"map={m}" for m in maps])
    return call(base, f"/api/v1/architecture/cross-repo?{q}")


def test_name_match_is_inferred_and_separate_from_verified(svc_api):
    base, ids, _ = svc_api
    st, out = xr(base, ids, ["app", "lib"])
    assert st == 200 and not out["verified"]
    edges = {(e["from"]["node_id"], e["to"]["node_id"]): e for e in out["inferred"]}
    e = edges[("module:app.main", "module:mylib.core")]
    assert e["inferred"] is True and e["resolution"] == "name-match" and e["kind"] == "depends_on"
    assert e["source_span"] == {"path": "app/main.py", "line_start": 2} and e["to"]["repository_id"] == LIB
    assert any(w for w in out["warnings"])


def test_stdlib_names_are_never_candidates_and_unknown_roots_are_reported_not_linked(svc_api):
    base, ids, _ = svc_api
    out = xr(base, ids, ["app", "lib"])[1]
    assert not [e for e in out["edges"] if e["to"]["node_id"] in ("module:os", "module:json")]
    roots = dict(map(tuple, out["unresolved_external_roots"]))
    assert "requests" in roots and "os" not in roots


def test_explicit_mapping_makes_a_verified_edge(svc_api):
    base, ids, _ = svc_api
    out = xr(base, ids, ["app", "lib", "other"], [f"{APP}|mylib|{LIB}", f"{APP}|vendored|{OTHER}"])[1]
    ok = {(e["from"]["node_id"], e["to"]["node_id"]) for e in out["verified"]}
    assert ("module:app.main", "module:mylib.core") in ok and ("module:app.b", "module:vendored.thing") in ok
    assert all(e["inferred"] is False and e["resolution"] == "mapping" for e in out["verified"])


def test_name_provided_by_two_repositories_is_ambiguous_not_guessed(svc_api):
    base, ids, _ = svc_api
    out = xr(base, ids, ["app", "lib", "other"])[1]
    assert out["ambiguous"] and {a["name"].split(".")[0] for a in out["ambiguous"]} == {"mylib"}
    assert not [e for e in out["edges"] if e["to"]["node_id"].startswith("module:mylib")]
    assert [e for e in out["inferred"] if e["to"]["node_id"] == "module:vendored.thing"]    # unambiguous one still links


def test_cross_repo_validation_and_guardrails(svc_api, trio):
    base, ids, _ = svc_api
    assert xr(base, ids, ["app"])[0] == 400
    assert call(base, f"/api/v1/architecture/cross-repo?snapshot={ids['app']}&snapshot=arch_nope")[0] == 404
    assert xr(base, ids, ["app", "lib"], [f"{APP}|os|{LIB}"])[0] == 400                    # cannot claim stdlib
    assert xr(base, ids, ["app", "lib"], [f"{APP}|mylib|github.com/x/y"])[0] == 400          # provider not in set
    assert xr(base, ids, ["app", "lib"], ["bad"])[0] == 400
    with pytest.raises(ValueError):
        crossrepo.resolve([(trio["app"]["snapshot"], None)] * 2)                                # one snapshot per repository


def test_edge_and_time_limits_mark_incomplete(svc_api, monkeypatch):
    base, ids, svr = svc_api
    monkeypatch.setattr(crossrepo, "MAX_EDGES", 1)
    out = xr(base, ids, ["app", "lib", "other"], [f"{APP}|mylib|{LIB}", f"{APP}|vendored|{OTHER}"])[1]
    assert out["incomplete"] is True and len(out["edges"]) == 1


# ------------------------------------------------------------------ explanations
class Fake(http.server.BaseHTTPRequestHandler):
    seen = []
    reply = "It imports app.util [e_aaaaaaaaaaaaaaaaaaaa] and [REAL]."

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append({"body": body, "auth": self.headers.get("Authorization")})
        facts = json.loads(body["messages"][1]["content"].split("\n", 1)[1])
        real = facts["edges"][0]["id"] if facts["edges"] else "e_" + "0" * 20
        out = json.dumps({"choices": [{"message": {"content": Fake.reply.replace("[REAL]", f"[{real}]")}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a): pass


@pytest.fixture
def llm():
    Fake.seen.clear()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"
    srv.shutdown()


def test_explain_is_off_by_default_and_makes_no_network_call(trio, llm):
    svr, _, base = start(architecture_enabled=True)
    try:
        call(base, "/api/v1/architecture/snapshots", trio["app"])
        sid = trio["app"]["snapshot"]["snapshot_id"]
        st, body = call(base, f"/api/v1/architecture/snapshots/{sid}/nodes/module:app.main/explain", {})
        assert st == 404 and "not configured" in body["error"] and Fake.seen == []
        assert "architecture_explain_v1" not in call(base, "/api/v1/health")[1]["capabilities"]
    finally:
        svr.shutdown()


def test_explain_sends_facts_only_never_source_and_verifies_citations(trio, llm, tmp_path):
    keyf = tmp_path / "k"; keyf.write_text("sekret\n")
    svr, _, base = start(architecture_enabled=True, architecture_explain={
        "enabled": True, "url": llm, "model": "m1", "api_key_file": str(keyf)})
    try:
        call(base, "/api/v1/architecture/snapshots", trio["app"])
        sid = trio["app"]["snapshot"]["snapshot_id"]
        assert "architecture_explain_v1" in call(base, "/api/v1/health")[1]["capabilities"]
        st, out = call(base, f"/api/v1/architecture/snapshots/{sid}/nodes/module:app.c/explain", {})
        assert st == 200 and out["inferred"] is True and out["source_sent"] is False and out["model"] == "m1"
        sent = Fake.seen[0]
        assert sent["auth"] == "Bearer sekret"
        wire = json.dumps(sent["body"])
        assert "import mylib.core" not in wire and "import requests" not in wire       # no source text left the box
        assert "app/c.py" in wire                                                    # derived facts did
        assert out["dropped_citations"] == ["e_aaaaaaaaaaaaaaaaaaaa"] and "e_aaaaaaaaaaaaaaaaaaaa" not in out["explanation"]
        assert len(out["cited_edges"]) == 1 and out["cited_edges"][0].startswith("e_")          # the real one survives
        assert f"[{out['cited_edges'][0]}]" in out["explanation"]
        assert call(base, f"/api/v1/architecture/snapshots/{sid}/nodes/ghost/explain", {})[0] == 404
    finally:
        svr.shutdown()


def test_explain_requires_operator_and_failures_do_not_leak(trio, tmp_path):
    svr, _, base = start(api_keys={"o": "operator", "v": "viewer"}, architecture_enabled=True,
                         architecture_explain={"enabled": True, "url": "http://127.0.0.1:9/x", "model": "m"})
    try:
        call(base, "/api/v1/architecture/snapshots", trio["app"], key="o")
        sid = trio["app"]["snapshot"]["snapshot_id"]
        p = f"/api/v1/architecture/snapshots/{sid}/nodes/module:app.main/explain"
        assert call(base, p, {}, key="v")[0] == 403
        st, body = call(base, p, {}, key="o")
        assert st == 502 and body["error"] == "explanation route failed: URLError"       # type only: no host, no detail
    finally:
        svr.shutdown()


def test_explain_config_is_strict():
    ok = explain.parse_config({"enabled": True, "url": "https://llm.example/v1/chat/completions", "model": "m"})
    assert ok["timeout"] == 60.0
    assert explain.parse_config({"enabled": False, "url": "garbage"}) is None
    assert explain.parse_config(None) is None
    for bad in ({"enabled": True, "url": "http://llm.example/x", "model": "m"},      # plain http to a remote host
                {"enabled": True, "url": "https://x/y"},                              # no model
                {"enabled": True, "url": "file:///etc/passwd", "model": "m"},
                {"enabled": True, "url": "https://x/y", "model": "m", "typo": 1},
                {"enabled": "yes"}):
        with pytest.raises(ValueError):
            explain.parse_config(bad)


def test_explain_refuses_redirects(trio, tmp_path):
    class Redir(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(307); self.send_header("Location", "http://127.0.0.1:1/steal"); self.end_headers()
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redir)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        arch = ArchitectureService(ControlPlaneRepo(), enabled=True)
        arch.publish(trio["app"])
        arch.explain_config = explain.parse_config({"enabled": True, "url": f"http://127.0.0.1:{srv.server_address[1]}/x", "model": "m"})
        with pytest.raises(ArchitectureError) as e:
            arch.explain(trio["app"]["snapshot"]["snapshot_id"], "module:app.main")
        assert e.value.status == 502
    finally:
        srv.shutdown()
