import http.client
import threading

import pytest

from herdr_engineering.control_plane import api
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


class _Sink:
    def record(self, event):
        pass

    def record_required(self, event):
        pass


_SERVERS = {}


@pytest.fixture()
def port(monkeypatch):
    monkeypatch.setattr(api, "_SSE_IDLE_SECONDS", 0.2)
    svr = ControlPlaneServer(ControlPlaneRepo(sink=_Sink()))
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    _SERVERS[svr.port] = svr
    yield svr.port
    svr.shutdown()


def test_idle_stream_stays_open_and_emits_keepalive_frames(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/v1/events/stream")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("X-Accel-Buffering") == "no"
    assert resp.readline().startswith(b"data: ")  # StreamConnected
    assert resp.readline() == b"\n"
    # several idle intervals pass; the stream must keep emitting, not close
    for _ in range(3):
        assert resp.readline() == b": keep-alive\n"
        assert resp.readline() == b"\n"
    conn.close()


def _connect(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/api/v1/events/stream")
    resp = conn.getresponse()
    assert resp.readline().startswith(b"data: ")
    assert resp.readline() == b"\n"
    return conn, resp


def _drain_until_eof(resp):
    while resp.readline():
        pass


def test_slow_subscriber_dropped_from_broadcast_closes_its_stream(port):
    # Reach the running server through the module-level fixture port.
    svr = _SERVERS[port]
    conn, resp = _connect(port)
    assert len(svr._subscribers) == 1
    (q,) = list(svr._subscribers)
    while True:  # fill the queue so the next broadcast drops it
        try:
            q.put_nowait({"event_type": "X", "payload": {}})
        except Exception:
            break
    svr._broadcast({"event_type": "Y", "payload": {}})
    assert q not in svr._subscribers
    _drain_until_eof(resp)  # stream must end rather than loop forever
    conn.close()


def test_stream_lifetime_is_capped(port, monkeypatch):
    monkeypatch.setattr(api, "_MAX_SSE_SECONDS", 0.5)
    conn, resp = _connect(port)
    _drain_until_eof(resp)
    conn.close()
