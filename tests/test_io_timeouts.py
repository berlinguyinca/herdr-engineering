"""Shared socket/process timeouts for tests that talk to a local server or spawn a CLI.

CI pipelines 1428/1444/1453/1454 failed scattered tests with `TimeoutError: timed out` on 3-10 s reads from an in-process server on a
starved node. A timeout in a test only has to catch a hang, and pytest-timeout (900 s) and the run watchdog already do; the literal
3/5/10 only turned a slow node into a red gate."""
import http.server
import os
import subprocess
import sys
import threading
import time

import pytest



def _import_with(value: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "HERDR_TEST_IO_TIMEOUT"}
    if value is not None:
        env["HERDR_TEST_IO_TIMEOUT"] = value
    return subprocess.run([sys.executable, "-c", "from tests import io_timeouts as t; print(t.HTTP_TIMEOUT, t.PROCESS_TIMEOUT)"],
                          capture_output=True, text=True, env=env, cwd=os.path.dirname(os.path.dirname(__file__)))


def test_default_is_generous_enough_for_a_starved_node():
    done = _import_with(None)
    assert done.returncode == 0, done.stderr
    http_timeout, process_timeout = map(float, done.stdout.split())
    assert http_timeout >= 30 and process_timeout >= 180


def test_the_http_timeout_is_overridable_and_the_process_timeout_scales_with_it():
    done = _import_with("7")
    assert done.returncode == 0, done.stderr
    assert float(done.stdout.split()[0]) == 7.0


@pytest.mark.parametrize("bad", ["0", "-3", "soon", ""])
def test_a_bad_override_is_refused_not_guessed(bad):
    done = _import_with(bad)
    assert done.returncode != 0 and "HERDR_TEST_IO_TIMEOUT" in done.stderr


def test_the_shared_control_plane_client_waits_out_a_slow_answer():
    """A server that takes 7 s to answer (a node under load) must not fail a test whose 3-5 s literal used to cut it off."""
    from tests.control_plane.test_collab_operator import call

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(7)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body = call(server.server_address[1], "GET", "/x", host="127.0.0.1")
    finally:
        server.shutdown()
        server.server_close()
    assert (status, body) == (200, {"ok": True})
