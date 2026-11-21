"""A client that resets or abandons a connection is routine on a busy control plane; it must not print a traceback."""
import socket
import struct
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler

from herdr_engineering.control_plane.api import _DaemonThreadingServer


class _Slow(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        time.sleep(0.3)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


def _serve():
    srv = _DaemonThreadingServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _reset_client(port):
    s = socket.create_connection(("127.0.0.1", port))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close => RST
    s.close()


def test_client_reset_logs_no_traceback_to_stderr(capfd):
    srv = _serve()
    try:
        for _ in range(5):
            _reset_client(srv.server_address[1])
        time.sleep(0.5)
        assert "Traceback" not in capfd.readouterr().err
    finally:
        srv.shutdown()


def test_client_broken_pipe_on_response_logs_no_traceback(capfd):
    srv = _serve()
    try:
        s = socket.create_connection(("127.0.0.1", srv.server_address[1]))
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
        time.sleep(0.8)
        assert "Traceback" not in capfd.readouterr().err
    finally:
        srv.shutdown()


def test_a_real_handler_bug_still_logs_its_traceback(capfd):
    class Boom(_Slow):
        def do_GET(self):  # noqa: N802
            raise RuntimeError("handler bug")

    srv = _DaemonThreadingServer(("127.0.0.1", 0), Boom)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=3)
        except Exception:
            pass
        time.sleep(0.2)
        assert "RuntimeError: handler bug" in capfd.readouterr().err
    finally:
        srv.shutdown()
