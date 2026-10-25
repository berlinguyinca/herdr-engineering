# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Room files: a room-scoped shelf of immutable, versioned uploads that agents fetch to a stable path.

Cast (from test_collab_tasks.W): alice owns the room; bob is a contributor; carol a viewer; dave a
guest; mallory is signed in but in no room."""
from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.collab import (
    CollabService,
    FileEventLog,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.collab_files import (
    DirectoryBlobStore,
    clean_file_name,
    normalize_files,
)
from tests.control_plane.test_collab import Clock, _digest
from tests.control_plane.test_collab_tasks import USERS, W, code, svc  # noqa: F401

MiB = 1024 * 1024


@pytest.fixture
def w(svc):
    return W(svc)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ upload, fetch, versions
def test_upload_then_fetch_returns_identical_bytes_and_hash(w):
    data = b"\x00\x01binary\xffpayload" * 100
    f = w.s.put_file(w.room, w.bob, "notes.bin", data)
    assert f == {"id": f["id"], "name": "notes.bin", "version": 1, "size": len(data), "sha256": sha(data)}
    meta, got = w.s.get_file(w.alice, w.room, f["id"])
    assert got == data and meta["sha256"] == sha(data) and meta["version"] == 1
    row = w.s.list_files(w.alice, w.room)["files"][0]
    assert row["path"] == ".collab/files/notes.bin"
    assert row["uploaded_by"] == {"type": "user", "id": "bob", "owner_user_id": "bob"}


def test_a_new_version_never_overwrites_the_old_one(w):
    v1 = w.s.put_file(w.room, w.bob, "plan.md", b"first")
    v2 = w.s.put_file(w.room, w.alice, "plan.md", b"second", parent=v1["id"])
    assert (v2["id"], v2["version"], v2["name"]) == (v1["id"], 2, "plan.md")
    assert w.s.get_file(w.bob, w.room, v1["id"], version=1)[1] == b"first"
    assert w.s.get_file(w.bob, w.room, v1["id"])[1] == b"second"            # the stable path is the latest
    row = w.s.list_files(w.bob, w.room)["files"][0]
    assert [v["version"] for v in row["versions"]] == [1, 2]
    assert [v["by"]["id"] for v in row["versions"]] == ["bob", "alice"]


def test_the_same_name_without_a_parent_is_a_new_file_with_a_distinct_name(w):
    a = w.s.put_file(w.room, w.bob, "report.pdf", b"one")
    b = w.s.put_file(w.room, w.alice, "report.pdf", b"two")
    c = w.s.put_file(w.room, w.alice, "report.pdf", b"three")
    assert a["id"] != b["id"] != c["id"]
    assert [b["name"], c["name"]] == ["report (2).pdf", "report (3).pdf"]
    assert w.s.get_file(w.bob, w.room, a["id"])[1] == b"one"


def test_versions_are_numbered_by_the_server_even_when_two_edits_race(w):
    v1 = w.s.put_file(w.room, w.bob, "x.txt", b"1")
    out = []
    ts = [threading.Thread(target=lambda d=d: out.append(w.s.put_file(w.room, w.bob, "x.txt", d, parent=v1["id"])))
          for d in (b"2", b"3")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(f["version"] for f in out) == [2, 3]


def test_a_parent_must_be_a_live_file_of_the_same_room(w):
    other = w.s.put_file(w.room2, w.alice, "o.txt", b"o")
    assert code(w.s.put_file, w.room, w.alice, "o.txt", b"x", parent=other["id"]) == 404
    assert code(w.s.put_file, w.room, w.alice, "o.txt", b"x", parent="file_nope") == 404


# ------------------------------------------------------------------ names
@pytest.mark.parametrize("raw,clean", [("a/b/c.txt", "c.txt"), ("C:\\x\\y.csv", "y.csv"), ("  spaced.md ", "spaced.md"),
                                       (".env", ".env"), ("a..b", "a..b")])
def test_names_lose_their_directories(raw, clean):
    assert clean_file_name(raw) == clean


@pytest.mark.parametrize("raw", ["", "   ", ".", "..", "../etc/passwd", "a/../b", "x\x00y", "tab\there", "nl\nx",
                                 "a/", "x" * 256, 7, "evil\u202efdp.exe", "zero\u200bwidth.txt"])
def test_bad_names_are_refused(w, raw):
    assert code(w.s.put_file, w.room, w.bob, raw, b"x") == 400


# ------------------------------------------------------------------ limits
def test_an_oversize_file_is_refused_with_a_clear_error(w):
    w.s.files_limits = normalize_files({"max_bytes": 10, "room_bytes": 100})
    with pytest.raises(Exception) as e:
        w.s.put_file(w.room, w.bob, "big.bin", b"x" * 11)
    assert e.value.status == 413 and "10 bytes" in e.value.message and "per file" in e.value.message


def test_the_room_quota_is_enforced_and_deletion_reclaims_it(w):
    w.s.files_limits = normalize_files({"max_bytes": 60, "room_bytes": 100})
    a = w.s.put_file(w.room, w.bob, "a.bin", b"x" * 60)
    with pytest.raises(Exception) as e:
        w.s.put_file(w.room, w.bob, "b.bin", b"y" * 50)
    assert e.value.status == 413 and "room" in e.value.message and "delete" in e.value.message.lower()
    w.s.put_file(w.room2, w.alice, "b.bin", b"y" * 50)               # another room has its own quota
    assert w.s.list_files(w.bob, w.room)["used_bytes"] == 60
    w.s.delete_file(w.bob, w.room, a["id"])
    assert w.s.list_files(w.bob, w.room)["used_bytes"] == 0
    w.s.put_file(w.room, w.bob, "b.bin", b"y" * 50)


def test_defaults_and_yaml_validation():
    assert normalize_files(None) == {"max_bytes": 25 * MiB, "room_bytes": 500 * MiB}
    for bad in ({"max_bytes": 0}, {"max_bytes": "1"}, {"room_bytes": -1}, {"max_bytes": 10, "room_bytes": 5},
                {"other": 1}, "x", {"max_bytes": True}):
        with pytest.raises(ValueError):
            normalize_files(bad)


# ------------------------------------------------------------------ permissions
def test_non_members_guests_and_external_agents_cannot_read(w):
    f = w.s.put_file(w.room, w.bob, "s.txt", b"secret")
    assert code(w.s.get_file, w.mallory, w.room, f["id"]) == 404
    assert code(w.s.list_files, w.mallory, w.room) == 404
    assert code(w.s.get_file, w.dave, w.room, f["id"]) == 403
    assert code(w.s.list_files, w.dave, w.room) == 403
    assert code(w.s.get_file, w.alice, w.room2, f["id"]) == 404        # a file id is scoped to its room
    assert w.s.get_file(w.carol, w.room, f["id"])[1] == b"secret"      # a viewer may read


def test_read_only_members_cannot_upload(w):
    assert code(w.s.put_file, w.room, w.carol, "v.txt", b"x") == 403
    assert code(w.s.put_file, w.room, w.dave, "v.txt", b"x") == 403
    assert code(w.s.put_file, w.room, w.mallory, "v.txt", b"x") == 404


def test_an_agent_put_creates_a_version_attributed_to_that_agent(w):
    agent, info = w.agent("bob")
    v1 = w.s.put_file(w.room, w.alice, "data.csv", b"a,b\n")
    v2 = w.s.put_file(w.room, agent, "data.csv", b"a,b\n1,2\n", parent=v1["id"])
    assert v2["version"] == 2
    row = w.s.list_files(agent, w.room)["files"][0]
    assert row["versions"][1]["by"] == {"type": "agent", "id": info["agent_id"], "owner_user_id": "bob"}
    assert w.s.get_file(agent, w.room, v1["id"])[1] == b"a,b\n1,2\n"


def test_only_the_uploader_or_a_room_admin_deletes(w):
    f = w.s.put_file(w.room, w.bob, "d.txt", b"x")
    agent, _ = w.agent("bob")
    assert code(w.s.delete_file, w.carol, w.room, f["id"]) == 403
    assert code(w.s.delete_file, agent, w.room, f["id"]) == 403       # bob's agent is not bob
    assert code(w.s.delete_file, w.mallory, w.room, f["id"]) == 404
    g = w.s.put_file(w.room, w.bob, "e.txt", b"y")
    w.s.delete_file(w.alice, w.room, g["id"])                          # the room owner
    w.s.delete_file(w.bob, w.room, f["id"])                            # the uploader
    assert w.s.list_files(w.bob, w.room)["files"] == []
    assert code(w.s.get_file, w.bob, w.room, f["id"]) == 404
    h = w.s.put_file(w.room, w.bob, "d.txt", b"z")                     # the name is free again
    assert h["name"] == "d.txt"


def test_shelf_rows_say_who_may_delete(w):
    w.s.put_file(w.room, w.bob, "d.txt", b"x")
    assert w.s.list_files(w.bob, w.room)["files"][0]["can_delete"] is True
    assert w.s.list_files(w.alice, w.room)["files"][0]["can_delete"] is True
    assert w.s.list_files(w.carol, w.room)["files"][0]["can_delete"] is False


def test_executables_and_archives_are_flagged_download_only(w):
    for n in ("tool.exe", "run.sh", "bundle.tar.gz", "x.zip"):
        w.s.put_file(w.room, w.bob, n, b"MZ")
    w.s.put_file(w.room, w.bob, "readme.md", b"# hi")
    kinds = {r["name"]: r["kind"] for r in w.s.list_files(w.bob, w.room)["files"]}
    assert kinds == {"tool.exe": "executable", "run.sh": "executable", "bundle.tar.gz": "archive",
                     "x.zip": "archive", "readme.md": "file"}


def test_blobs_are_shared_by_content_and_only_dropped_when_unreferenced(w):
    a = w.s.put_file(w.room, w.bob, "a.txt", b"same")
    w.s.put_file(w.room2, w.alice, "b.txt", b"same")
    w.s.delete_file(w.bob, w.room, a["id"])
    f = w.s.list_files(w.alice, w.room2)["files"][0]
    assert w.s.get_file(w.alice, w.room2, f["id"])[1] == b"same"


# ------------------------------------------------------------------ messages carry files
def test_a_message_carries_server_derived_files(w):
    f = w.s.put_file(w.room, w.bob, "plan.md", b"plan")
    status, out = w.s.post_message(w.bob, w.room, {"text": "see plan", "idempotency_key": "k1",
                                                   "files": [{"id": f["id"], "name": "evil.exe", "size": 1,
                                                              "sha256": "0" * 64}]})
    assert status == 201
    assert out["event"]["files"] == [f]
    ev = w.s.read_events(w.alice, w.room, 0, 50)["events"][-1]
    assert ev["files"] == [f]


def test_a_message_may_be_only_files(w):
    f = w.s.put_file(w.room, w.bob, "plan.md", b"plan")
    status, out = w.s.post_message(w.bob, w.room, {"text": "", "idempotency_key": "k2", "files": [f["id"]]})
    assert status == 201 and out["event"]["files"] == [f]
    assert code(w.s.post_message, w.bob, w.room, {"text": "", "idempotency_key": "k3"}) == 400


def test_a_message_cannot_attach_a_file_of_another_room_or_a_missing_version(w):
    other = w.s.put_file(w.room2, w.alice, "o.txt", b"o")
    f = w.s.put_file(w.room, w.bob, "p.txt", b"p")
    for files in ([other["id"]], ["file_nope"], [{"id": f["id"], "version": 2}], "x", [f["id"]] * 11):
        assert code(w.s.post_message, w.bob, w.room, {"text": "x", "idempotency_key": "k4", "files": files}) == 400


def test_an_older_version_can_be_attached(w):
    f = w.s.put_file(w.room, w.bob, "p.txt", b"p")
    w.s.put_file(w.room, w.bob, "p.txt", b"q", parent=f["id"])
    _, out = w.s.post_message(w.bob, w.room, {"text": "v1", "idempotency_key": "k5",
                                              "files": [{"id": f["id"], "version": 1}]})
    assert out["event"]["files"] == [f]


# ------------------------------------------------------------------ delivery and durability
def test_new_files_reach_the_room_long_poll(w):
    seq = w.s.room_state(w.bob, w.room, -1, 0, 0)["seq"]
    w.s.put_file(w.room, w.alice, "n.txt", b"n")
    st = w.s.room_state(w.bob, w.room, seq, 0, 0)
    assert st["changed"] and [f["name"] for f in st["files"]] == ["n.txt"]
    assert w.s.room_state(w.dave, w.room, -1, 0, 0)["files"] is None     # guests get no shelf


def test_the_shelf_and_the_bytes_survive_a_restart(tmp_path):
    blobs = DirectoryBlobStore(str(tmp_path / "files"))
    auth = StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()})

    def boot():
        s = CollabService(auth, sink=FileEventLog(str(tmp_path / "log")), now=Clock())
        s.file_blobs = blobs
        s.hydrate(s._sink.load())
        return s

    s = boot()
    room = W(s)
    f = s.put_file(room.room, room.bob, "keep.txt", b"kept")
    s.put_file(room.room, room.bob, "keep.txt", b"kept2", parent=f["id"])
    s2 = boot()
    bob = s2.authenticate(f"Bearer {USERS['bob']}", None)
    assert s2.get_file(bob, room.room, f["id"], version=1)[1] == b"kept"
    assert s2.list_files(bob, room.room)["used_bytes"] == 9


def test_directory_store_is_content_addressed_and_verifies(tmp_path):
    store = DirectoryBlobStore(str(tmp_path / "b"))
    key, digest = store.put(b"abc")
    assert digest == sha(b"abc") and key == f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
    assert store.get(key, digest) == b"abc"
    with pytest.raises(Exception):
        store.get(key, "0" * 64)
    with pytest.raises(Exception):
        store.get("../../etc/passwd", digest)
    store.delete(key)
    with pytest.raises(Exception):
        store.get(key, digest)


# ------------------------------------------------------------------ HTTP
@pytest.fixture
def http(svc):
    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    room = W(svc)
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc, api_keys={"op": "operator"})
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    yield type("H", (), dict(base=f"http://127.0.0.1:{srv.port}/api/v1/collab", room=room, s=svc))
    srv.shutdown()
    t.join(timeout=5)


def req(method, url, data=None, user="bob", headers=None):
    h = {"Authorization": f"Bearer {USERS[user]}", **(headers or {})}
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_http_upload_is_raw_bytes_larger_than_a_json_body(http):
    data = bytes(range(256)) * 1024                                   # 256 KiB: well over the 64 KiB JSON cap
    url = f"{http.base}/rooms/{http.room.room}/files?name=blob.bin"
    st, _, body = req("POST", url, data, headers={"Content-Type": "application/octet-stream"})
    assert st == 201, body
    f = json.loads(body)["file"]
    assert f["sha256"] == sha(data) and f["size"] == len(data)
    st, h, got = req("GET", f"{http.base}/rooms/{http.room.room}/files/{f['id']}", user="carol")
    assert st == 200 and got == data
    assert h["Content-Type"] == "application/octet-stream"
    assert h["Content-Disposition"].startswith("attachment;") and "blob.bin" in h["Content-Disposition"]
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Herdr-File-Sha256"] == sha(data) and h["X-Herdr-File-Version"] == "1"
    st, _, body = req("POST", f"{http.base}/rooms/{http.room.room}/files/{f['id']}/versions", b"v2",
                      headers={"Content-Type": "application/octet-stream"})
    assert st == 201 and json.loads(body)["file"]["version"] == 2
    st, _, got = req("GET", f"{http.base}/rooms/{http.room.room}/files/{f['id']}?v=1")
    assert got == data
    st, _, body = req("GET", f"{http.base}/rooms/{http.room.room}/files")
    assert st == 200 and json.loads(body)["files"][0]["version"] == 2
    st, _, body = req("POST", f"{http.base}/rooms/{http.room.room}/files/{f['id']}/delete", b"{}",
                      headers={"Content-Type": "application/json"})
    assert st == 200, body


def test_http_upload_refuses_form_posts_cross_origin_and_oversize(http):
    url = f"{http.base}/rooms/{http.room.room}/files?name=a.txt"
    assert req("POST", url, b"x", headers={"Content-Type": "multipart/form-data; boundary=x"})[0] == 415
    assert req("POST", url, b"x", headers={"Content-Type": "application/octet-stream",
                                           "Origin": "https://evil.example"})[0] == 403
    http.s.files_limits = normalize_files({"max_bytes": 4, "room_bytes": 100})
    st, _, body = req("POST", url, b"12345", headers={"Content-Type": "application/octet-stream"})
    assert st == 413 and b"per file" in body
    assert req("GET", f"{http.base}/rooms/{http.room.room}/files", user="mallory")[0] == 404
    assert req("GET", f"{http.base}/rooms/{http.room.room}/files", user="dave")[0] == 403


def test_the_dashboard_proxy_carries_uploads_and_downloads(tmp_path):
    import http.client

    from tests.control_plane.test_collab_operator import (
        stack as op_stack,  # noqa: F401  (fixture body reused)
    )
    gen = op_stack.__wrapped__(tmp_path)
    st = next(gen)
    try:
        d = st.dash.port

        def dash(method, path, data=None, ctype=None):
            conn = http.client.HTTPConnection("127.0.0.1", d, timeout=10)
            conn.request(method, path, body=data, headers={"Host": f"127.0.0.1:{d}",
                                                            **({"Content-Type": ctype} if ctype else {})})
            r = conn.getresponse()
            out = (r.status, dict(r.getheaders()), r.read())
            conn.close()
            return out

        s, _, body = dash("POST", "/api/v1/collab/projects", b'{"name":"p"}', "application/json")
        pid = json.loads(body)["project_id"]
        s, _, body = dash("POST", f"/api/v1/collab/projects/{pid}/rooms", b'{"name":"r"}', "application/json")
        rid = json.loads(body)["room_id"]
        data = b"z" * (3 * 1024 * 1024)                                      # over the proxy's 2 MB JSON read
        s, _, body = dash("POST", f"/api/v1/collab/rooms/{rid}/files?name=big.bin", data, "application/octet-stream")
        assert s == 201, body
        fid = json.loads(body)["file"]["id"]
        s, h, got = dash("GET", f"/api/v1/collab/rooms/{rid}/files/{fid}")
        assert s == 200 and got == data
        assert h["Content-Type"] == "application/octet-stream" and h["X-Content-Type-Options"] == "nosniff"
        assert h["Content-Disposition"].startswith("attachment;")
        assert dash("POST", f"/api/v1/collab/rooms/{rid}/files?name=a", b"x", "text/plain")[0] == 415
    finally:
        gen.close()


# ------------------------------------------------------------------ the agent helper (herdr-eng collab file)
def test_agent_helper_lists_gets_and_puts_versions_into_its_working_directory(http, tmp_path, capsys, monkeypatch):
    from herdr_engineering import cli
    agent, info = http.room.agent("bob")
    af = tmp_path / "agent.json"
    af.write_text(json.dumps({"credential": info["credential"], "device_id": info["device_id"], "name": "x"}))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    base = http.base.rsplit("/api/v1/collab", 1)[0]
    common = ["--agent-file", str(af), "--base", base, "--room", http.room.room]
    f = http.s.put_file(http.room.room, http.room.alice, "spec.md", b"# v1\n")

    assert cli.main(["collab", "file", "list", *common]) == 0
    assert "spec.md" in capsys.readouterr().out
    assert cli.main(["collab", "file", "get", "spec.md", *common]) == 0
    target = work / ".collab" / "files" / "spec.md"
    assert target.read_bytes() == b"# v1\n"
    assert info["credential"] not in capsys.readouterr().out                   # the credential is never printed

    target.write_bytes(b"# v2 by the agent\n")                                 # the agent edits the stable path
    assert cli.main(["collab", "file", "put", str(target), *common]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["id"], out["version"]) == (f["id"], 2)                         # an edit is a new version
    row = http.s.list_files(http.room.alice, http.room.room)["files"][0]
    assert row["versions"][1]["by"]["id"] == info["agent_id"]
    assert http.s.get_file(http.room.alice, http.room.room, f["id"], version=1)[1] == b"# v1\n"

    new = work / "other.txt"
    new.write_bytes(b"o")
    assert cli.main(["collab", "file", "put", str(new), *common]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 1
    assert cli.main(["collab", "file", "get", "spec.md", "--version", "1", *common]) == 0
    assert target.read_bytes() == b"# v1\n"


def test_agent_helper_never_writes_outside_the_files_folder(tmp_path):
    from herdr_engineering.cli import _collab_file_target
    root = tmp_path / "w"
    assert _collab_file_target(str(root), "a.txt") == root / ".collab" / "files" / "a.txt"
    for bad in ("../x", "..", "a/../../b", "x\x00"):
        with pytest.raises(SystemExit):
            _collab_file_target(str(root), bad)


@pytest.mark.parametrize("base", ["http://collab.example.org", "http://localhost.attacker.tld", "http://127.0.0.1.evil.example",
                                  "https://user:pw@collab.example.org", "ftp://x", "", "https://x/path"])
def test_agent_helper_sends_its_credential_only_over_https_or_to_this_machine(tmp_path, base):
    from herdr_engineering import cli
    af = tmp_path / "agent.json"
    af.write_text(json.dumps({"credential": "c", "device_id": "d"}))
    with pytest.raises(SystemExit) as e:
        cli.main(["collab", "file", "list", "--agent-file", str(af), "--base", base, "--room", "r"])
    assert "--base" in str(e.value)
