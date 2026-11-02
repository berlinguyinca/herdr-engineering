"""herdr socket client against a fake unix-socket server speaking herdr's real envelope.

Envelope (protocol 22, confirmed on bender): one JSON object per line, request
{"id","method","params"}, reply {"id","result"} or {"id","error":{"code","message"}}.
"""
import json
import os
import socket
import tempfile
import threading
import time

import pytest

from herdr_engineering.herdr_pane_client import HerdrError, HerdrPaneClient


class FakeHerdr:
    def __init__(self, handler):
        self.dir = tempfile.mkdtemp(prefix="hpc")  # short path: AF_UNIX limit on macOS
        self.path = os.path.join(self.dir, "s.sock")
        self.handler = handler
        self.requests = []
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)
        self.sock.listen(8)
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf = b""
        while b"\n" not in buf:
            d = c.recv(65536)
            if not d:
                return
            buf += d
        req = json.loads(buf.split(b"\n")[0])
        self.requests.append(req)
        for line in self.handler(req, c):
            c.sendall(json.dumps(line).encode() + b"\n")
        if req["method"] != "events.subscribe":
            c.close()

    def close(self):
        self._stop = True
        self.sock.close()


def ok(req, result):
    return [{"id": req["id"], "result": result}]


@pytest.fixture
def fake():
    made = []

    def make(handler):
        f = FakeHerdr(handler)
        made.append(f)
        return f

    yield make
    for f in made:
        f.close()


PANE = {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "agent": "claude",
        "agent_status": "idle", "scroll": {"viewport_rows": 40}}


def test_list_panes_sends_envelope_and_returns_panes(fake):
    f = fake(lambda r, c: ok(r, {"type": "pane_list", "panes": [PANE]}))
    panes = HerdrPaneClient(f.path).list_panes()
    assert panes == [PANE]
    assert f.requests[0]["method"] == "pane.list" and f.requests[0]["params"] == {}
    assert isinstance(f.requests[0]["id"], str)


def test_layout_picks_the_pane_rect(fake):
    layout = {"type": "pane_layout", "layout": {"area": {"x": 0, "y": 0, "width": 120, "height": 40},
              "zoomed": False, "panes": [
                  {"pane_id": "w1:p2", "focused": False, "rect": {"x": 60, "y": 0, "width": 60, "height": 40}},
                  {"pane_id": "w1:p1", "focused": True, "rect": {"x": 0, "y": 0, "width": 60, "height": 38}}]}}
    f = fake(lambda r, c: ok(r, layout))
    out = HerdrPaneClient(f.path).layout("w1:p1")
    assert (out["width"], out["height"]) == (60, 38)
    assert out["rect"] == {"x": 0, "y": 0, "width": 60, "height": 38}
    assert f.requests[0]["params"] == {"pane_id": "w1:p1"}


def test_layout_missing_pane_is_an_error(fake):
    f = fake(lambda r, c: ok(r, {"type": "pane_layout", "layout": {"panes": []}}))
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(f.path).layout("w1:p1")
    assert e.value.code == "pane_not_in_layout"


def _read(text):
    return lambda r, c: ok(r, {"type": "pane_read", "read": {"pane_id": "w1:p1", "text": text}})


def test_read_visible_asks_for_raw_ansi(fake):
    f = fake(_read("\x1b[1mhi\x1b[0m"))
    assert HerdrPaneClient(f.path).read_visible("w1:p1") == "\x1b[1mhi\x1b[0m"
    assert f.requests[0]["method"] == "pane.read"
    assert f.requests[0]["params"] == {"pane_id": "w1:p1", "source": "visible",
                                       "format": "ansi", "strip_ansi": False}


def test_read_recent_passes_line_count(fake):
    f = fake(_read("a\nb\n"))
    assert HerdrPaneClient(f.path).read_recent("w1:p1", 50) == "a\nb\n"
    assert f.requests[0]["params"] == {"pane_id": "w1:p1", "source": "recent", "lines": 50}


def test_send_text_keys_input_zoom_resize_bodies(fake):
    f = fake(lambda r, c: ok(r, {"type": "ok"}))
    c = HerdrPaneClient(f.path)
    c.send_text("w1:p1", "hello é")
    c.send_keys("w1:p1", ["ctrl+c", "esc", "alt+b", "shift+tab", "up", "f1"])
    c.send_input("w1:p1", b"\x1b[H\x1b[3~")
    c.zoom("w1:p1", True)
    c.zoom("w1:p1", False)
    c.resize_split("w1:p1", "left", 0.1)
    got = [(r["method"], r["params"]) for r in f.requests]
    assert got == [
        ("pane.send_text", {"pane_id": "w1:p1", "text": "hello é"}),
        ("pane.send_keys", {"pane_id": "w1:p1", "keys": ["ctrl+c", "esc", "alt+b", "shift+tab", "up", "f1"]}),
        ("pane.send_input", {"pane_id": "w1:p1", "text": "\x1b[H\x1b[3~"}),  # raw bytes ride in text
        ("pane.zoom", {"pane_id": "w1:p1", "mode": "on"}),
        ("pane.zoom", {"pane_id": "w1:p1", "mode": "off"}),
        ("pane.resize", {"pane_id": "w1:p1", "direction": "left", "amount": 0.1}),
    ]


def test_error_response_raises_herdr_error(fake):
    f = fake(lambda r, c: [{"id": r["id"], "error": {"code": "pane_not_found", "message": "no such pane"}}])
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(f.path).read_visible("w1:p9")
    assert e.value.code == "pane_not_found" and "no such pane" in str(e.value)


@pytest.mark.parametrize("bad", ["", "../x", "w1:p1 ", "w1:p1\n", "-w1", "a" * 65, "w1;rm", None, 5])
def test_pane_id_charset_is_enforced_before_any_io(fake, bad):
    f = fake(lambda r, c: ok(r, {"type": "ok"}))
    cl = HerdrPaneClient(f.path)
    with pytest.raises(ValueError):
        cl.send_text(bad, "x")
    with pytest.raises(ValueError):
        cl.read_visible(bad)
    assert f.requests == []


@pytest.mark.parametrize("keys", [["ctrl-c"], ["Home"], ["End"], ["PageUp"], ["Delete"], ["C-d"],
                                  ["ctrl+C"], ["ctrl+cc"], [], "enter", ["enter", 5], ["x"] * 33])
def test_keys_outside_the_allow_list_are_refused(fake, keys):
    f = fake(lambda r, c: ok(r, {"type": "ok"}))
    with pytest.raises(ValueError):
        HerdrPaneClient(f.path).send_keys("w1:p1", keys)
    assert f.requests == []


def test_size_limits_and_types(fake):
    f = fake(lambda r, c: ok(r, {"type": "ok"}))
    cl = HerdrPaneClient(f.path)
    cl.send_input("w1:p1", b"a" * 4096)
    cl.send_text("w1:p1", "a" * 4096)
    for call in (lambda: cl.send_input("w1:p1", b"a" * 4097), lambda: cl.send_input("w1:p1", b""),
                 lambda: cl.send_input("w1:p1", "str"), lambda: cl.send_input("w1:p1", b"\xff\xfe"),
                 lambda: cl.send_text("w1:p1", "a" * 4097), lambda: cl.send_text("w1:p1", b"x"),
                 lambda: cl.zoom("w1:p1", "yes"), lambda: cl.resize_split("w1:p1", "diagonal", 0.1),
                 lambda: cl.resize_split("w1:p1", "left", 0), lambda: cl.resize_split("w1:p1", "left", 2),
                 lambda: cl.resize_split("w1:p1", "left", float("nan")),
                 lambda: cl.read_recent("w1:p1", 0), lambda: cl.read_recent("w1:p1", 100001)):
        with pytest.raises(ValueError):
            call()
    assert len(f.requests) == 2


def test_default_socket_path_and_env_override(monkeypatch, fake):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setenv("HOME", "/home/x")
    assert HerdrPaneClient().socket_path == "/home/x/.config/herdr/herdr.sock"
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/other.sock")
    assert HerdrPaneClient().socket_path == "/tmp/other.sock"
    assert HerdrPaneClient("/tmp/explicit.sock").socket_path == "/tmp/explicit.sock"


def test_silent_server_times_out(fake):
    def silent(req, c):
        time.sleep(2)
        return []
    f = fake(silent)
    t = time.monotonic()
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(f.path, timeout=0.3).list_panes()
    assert time.monotonic() - t < 1.5 and e.value.code == "timeout"


def test_missing_socket_is_a_herdr_error(tmp_path):
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(str(tmp_path / "nope.sock")).list_panes()
    assert e.value.code == "unavailable"


def test_subscribe_output_yields_events_then_idle_none(fake):
    event = {"event": "pane.output_matched", "data": {"pane_id": "w1:p1", "matched_line": "hi",
             "read": {"text": "hi\n", "revision": 3}}}

    def handler(req, c):
        yield {"id": req["id"], "result": {"type": "subscription_started"}}
        yield event
    f = fake(handler)
    it = HerdrPaneClient(f.path, timeout=0.3).subscribe_output("w1:p1")
    assert next(it) == event["data"]
    assert next(it) is None  # socket idle: caller falls back to polling the screen
    sub = f.requests[0]
    assert sub["method"] == "events.subscribe"
    s = sub["params"]["subscriptions"][0]
    assert s["type"] == "pane.output_matched" and s["pane_id"] == "w1:p1"
    assert s["source"] == "visible" and s["match"]["type"] == "regex"
    it.close()


def test_subscribe_refused_raises(fake):
    f = fake(lambda r, c: [{"id": r["id"], "error": {"code": "pane_not_found", "message": "gone"}}])
    with pytest.raises(HerdrError) as e:
        next(HerdrPaneClient(f.path).subscribe_output("w1:p1"))
    assert e.value.code == "pane_not_found"


def test_subscribe_server_close_raises(fake):
    def handler(req, c):
        yield {"id": req["id"], "result": {"type": "subscription_started"}}
        c.close()
    f = fake(handler)
    it = HerdrPaneClient(f.path, timeout=1).subscribe_output("w1:p1")
    with pytest.raises(HerdrError) as e:
        next(it)
    assert e.value.code == "closed"


# -- fix round 1 -------------------------------------------------------------------------------
def test_subscribe_validates_pane_id_eagerly(fake):
    f = fake(lambda r, c: ok(r, {"type": "ok"}))
    with pytest.raises(ValueError):  # at call time, before iteration
        HerdrPaneClient(f.path).subscribe_output("../x")
    assert f.requests == []


def test_send_text_rejects_empty(fake):
    f = fake(lambda r, c: ok(r, {"type": "ok"}))
    with pytest.raises(ValueError):
        HerdrPaneClient(f.path).send_text("w1:p1", "")
    assert f.requests == []


@pytest.mark.parametrize("result", [
    {"type": "pane_list", "panes": ["x"]},
    {"type": "pane_list", "panes": [1]},
])
def test_list_panes_malformed_is_bad_reply(fake, result):
    f = fake(lambda r, c: ok(r, result))
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(f.path).list_panes()
    assert e.value.code == "bad_reply"


@pytest.mark.parametrize("layout", [
    "str", ["a"], {"panes": "x"}, {"panes": ["x"]},
    {"panes": [{"pane_id": "w1:p1", "rect": {"x": 0}}]},
    {"panes": [{"pane_id": "w1:p1", "rect": {"width": "a", "height": 2}}]},
])
def test_layout_malformed_is_bad_reply(fake, layout):
    f = fake(lambda r, c: ok(r, {"type": "pane_layout", "layout": layout}))
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(f.path).layout("w1:p1")
    assert e.value.code == "bad_reply"


def test_reply_id_must_match(fake):
    f = fake(lambda r, c: [{"id": "other", "result": {"type": "pane_list", "panes": []}}])
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(f.path).list_panes()
    assert e.value.code == "bad_reply"


def test_event_before_subscription_started_is_not_the_reply(fake):
    event = {"event": "pane.output_matched", "data": {"pane_id": "w1:p1"}}

    def handler(req, c):
        yield event
        yield {"id": req["id"], "result": {"type": "subscription_started"}}
        yield event
    f = fake(handler)
    it = HerdrPaneClient(f.path, timeout=1).subscribe_output("w1:p1")
    assert next(it) == event["data"]
    it.close()


def test_error_message_is_truncated(fake):
    f = fake(lambda r, c: [{"id": r["id"], "error": {"code": "x" * 500, "message": "m" * 5000}}])
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(f.path).list_panes()
    assert len(e.value.message) <= 200 and len(e.value.code) <= 64


def test_socket_override_must_be_a_socket(tmp_path):
    p = tmp_path / "fake.sock"
    p.write_text("x")
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(str(p)).list_panes()
    assert e.value.code == "unavailable"


def test_socket_override_symlink_refused(fake):
    f = fake(lambda r, c: ok(r, {"type": "pane_list", "panes": []}))
    link = os.path.join(f.dir, "link.sock")
    os.symlink(f.path, link)
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(link).list_panes()
    assert e.value.code == "unavailable"


def test_socket_owned_by_someone_else_refused(fake, monkeypatch):
    f = fake(lambda r, c: ok(r, {"type": "pane_list", "panes": []}))
    monkeypatch.setattr(os, "getuid", lambda: os.stat(f.path).st_uid + 1)
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(f.path).list_panes()
    assert e.value.code == "unavailable"


def test_reply_over_cap_is_refused(fake):
    def handler(req, c):
        try:
            c.sendall(b"x" * (8 * 1024 * 1024 + 70000))
        except OSError:
            pass
        return []
    f = fake(handler)
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(f.path, timeout=5).list_panes()
    assert e.value.code == "bad_reply"


def test_slow_drip_hits_the_overall_deadline(fake):
    def handler(req, c):
        for _ in range(40):
            try:
                c.sendall(b" ")
            except OSError:
                break
            time.sleep(0.1)
        return []
    f = fake(handler)
    t = time.monotonic()
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(f.path, timeout=0.5).list_panes()
    assert time.monotonic() - t < 1.5 and e.value.code == "timeout"


def test_non_json_reply_is_bad_reply(fake):
    def handler(req, c):
        c.sendall(b"not json\n")
        return []
    f = fake(handler)
    with pytest.raises(HerdrError) as e:
        HerdrPaneClient(f.path).list_panes()
    assert e.value.code == "bad_reply"
