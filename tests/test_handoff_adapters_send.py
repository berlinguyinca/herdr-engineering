"""send_typed against a real AF_UNIX server speaking herdr's envelope (the pattern of test_herdr_pane_client.py)."""
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator

import pytest

from herdr_engineering.handoff_adapters.base import ExitPlan, TypedInput
from herdr_engineering.handoff_adapters.send import send_typed
from herdr_engineering.herdr_pane_client import HerdrPaneClient


class Recorder:
    """A unix-socket server that answers every request with an empty result and notes (monotonic time, method, params)."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="hsd")  # short path: AF_UNIX limit on macOS
        self.path = os.path.join(self.dir, "s.sock")
        self.seen: list[tuple[float, str, dict]] = []
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)
        self.sock.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buf = b""
        while b"\n" not in buf:
            data = conn.recv(65536)
            if not data:
                return
            buf += data
        request = json.loads(buf.split(b"\n")[0])
        self.seen.append((time.monotonic(), request["method"], request["params"]))
        conn.sendall(json.dumps({"id": request["id"], "result": {"type": "ok"}}).encode() + b"\n")
        conn.close()

    def close(self) -> None:
        self.sock.close()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def server() -> Iterator[Recorder]:
    recorder = Recorder()
    yield recorder
    recorder.close()


def test_text_is_sent_then_at_least_the_delay_passes_then_the_keys_lowercased(server):
    send_typed(HerdrPaneClient(server.path), "w1:p1", TypedInput("/compact", ("Enter",), submit_delay_s=0.5))
    (t_text, m_text, p_text), (t_keys, m_keys, p_keys) = server.seen
    assert (m_text, p_text) == ("pane.send_text", {"pane_id": "w1:p1", "text": "/compact"})
    assert (m_keys, p_keys) == ("pane.send_keys", {"pane_id": "w1:p1", "keys": ["enter"]})
    assert t_keys - t_text >= 0.5


def test_the_default_delay_is_the_measured_one(server):
    send_typed(HerdrPaneClient(server.path), "w1:p1", TypedInput("/exit", ("Enter",)))
    assert server.seen[1][0] - server.seen[0][0] >= TypedInput("", ()).submit_delay_s


def test_an_exit_plan_without_text_sends_only_its_keys(server):
    send_typed(HerdrPaneClient(server.path), "w1:p1", ExitPlan(None, ("Ctrl+C", "Ctrl+C"), 5.0))
    assert [(m, p) for _, m, p in server.seen] == [("pane.send_keys", {"pane_id": "w1:p1", "keys": ["ctrl+c", "ctrl+c"]})]


def test_a_zero_delay_does_not_sleep(server):
    send_typed(HerdrPaneClient(server.path), "w1:p1", TypedInput("x", ("Enter",), submit_delay_s=0.0))
    assert [m for _, m, _ in server.seen] == ["pane.send_text", "pane.send_keys"]
    assert server.seen[1][0] - server.seen[0][0] < 0.25


def test_text_without_keys_is_typed_and_not_submitted(server):
    send_typed(HerdrPaneClient(server.path), "w1:p1", TypedInput("draft", (), submit_delay_s=0.5))
    assert [m for _, m, _ in server.seen] == ["pane.send_text"]
