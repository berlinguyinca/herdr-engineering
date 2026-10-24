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


@pytest.fixture()
def port(monkeypatch):
    monkeypatch.setattr(api, "_SSE_IDLE_SECONDS", 0.2)
    svr = ControlPlaneServer(ControlPlaneRepo(sink=_Sink()))
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
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
