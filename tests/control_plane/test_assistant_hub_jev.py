"""Jev client: request shape, strict response validation, hostile-transport behavior.

There is no fake Jev server here.  The raw-socket listeners in this file exist ONLY to produce
hostile TRANSPORT behavior (accept and never answer, redirect, oversize stream, slow drip); they
never implement the System One protocol.  Tests that need a real System One are marked
``integration`` and skip unless HERDR_JEV_TEST_URL / HERDR_JEV_TEST_KEY_FILE are set.  Nothing here
touches real user config directories or production.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from herdr_engineering.control_plane.assistant_hub.jev import (
    ChoiceAnswer,
    JevClient,
    JudgeResult,
    JudgeUnavailable,
    NoulAnswer,
    ScoreAnswer,
    build_request,
    parse_response,
)

QUESTIONS: dict[str, dict[str, Any]] = {
    "destructive": {"type": "noul", "instructions": "Is it destructive?"},
    "severity": {"type": "score", "criteria": ["low", "mid", "high"]},
    "bucket": {"type": "choice", "criteria": {"billing": "pay", "technical": "tech", "sales": "buy"}},
}
EXPECTED = {"destructive": "noul", "severity": "score", "bucket": "choice"}

# documented sample, not captured: shapes from InferWeave docs/system-one/api.md
DOCUMENTED: dict[str, Any] = {
    "model": "laya-auto",
    "answers": {
        "destructive": {"type": "noul", "noul": 0.12},
        "severity": {
            "type": "score", "score": 1.78, "confidence": 0.88,
            "legend": {"0": "low", "1": "mid", "2": "high"},
            "probabilities": {"0": 0.04, "1": 0.14, "2": 0.82},
        },
        "bucket": {
            "type": "choice", "choice": "billing", "confidence": 0.91,
            "probabilities": {"billing": 0.91, "technical": 0.06, "sales": 0.03},
        },
    },
    "usage": {"input_tokens": 10, "output_tokens": 3},
}


def _body(doc: Any) -> bytes:
    return json.dumps(doc).encode()


def _mutated(path: list[str], value: Any) -> bytes:
    doc = json.loads(json.dumps(DOCUMENTED))
    cur = doc
    for p in path[:-1]:
        cur = cur[p]
    if value is KeyError:
        del cur[path[-1]]
    else:
        cur[path[-1]] = value
    return json.dumps(doc, allow_nan=True).encode()


# ------------------------------------------------------------ parse_response


def test_parse_documented_sample() -> None:
    res = parse_response(_body(DOCUMENTED), EXPECTED, QUESTIONS, request_id="req-1")
    assert res.model == "laya-auto" and res.request_id == "req-1"
    assert res.input_tokens == 10 and res.output_tokens == 3
    assert res.answers["destructive"] == NoulAnswer(p_yes=0.12)
    sev = res.answers["severity"]
    assert isinstance(sev, ScoreAnswer) and sev.expected == 1.78 and sev.probabilities["2"] == 0.82
    bucket = res.answers["bucket"]
    assert isinstance(bucket, ChoiceAnswer) and bucket.choice == "billing"
    assert parse_response(_body(DOCUMENTED), EXPECTED).answers.keys() == EXPECTED.keys()


@pytest.mark.parametrize(
    "body",
    [
        _mutated(["answers", "destructive", "type"], "score"),
        _mutated(["answers", "destructive"], KeyError),
        _mutated(["answers", "extra"], {"type": "noul", "noul": 0.1}),
        _mutated(["answers", "destructive", "noul"], float("nan")),
        _mutated(["answers", "destructive", "noul"], float("inf")),
        _mutated(["answers", "destructive", "noul"], -0.1),
        _mutated(["answers", "destructive", "noul"], 1.5),
        _mutated(["answers", "destructive", "noul"], True),
        _mutated(["answers", "destructive", "noul"], "0.5"),
        _mutated(["answers", "severity", "probabilities"], {"0": 0.1, "1": 0.1, "2": 0.1}),
        _mutated(["answers", "severity", "probabilities"], {"0": 0.5, "1": 0.5}),
        _mutated(["answers", "severity", "probabilities"], {"0": -0.2, "1": 0.6, "2": 0.6}),
        _mutated(["answers", "severity", "confidence"], 2),
        _mutated(["answers", "severity", "score"], 9),
        _mutated(["answers", "bucket", "probabilities"], {"billing": 0.5, "technical": 0.1, "sales": 0.1}),
        _mutated(["answers", "bucket", "probabilities"], {"billing": 0.9, "other": 0.1}),
        _mutated(["answers", "bucket", "choice"], "unknown"),
        _mutated(["usage", "input_tokens"], -1),
        _mutated(["usage", "output_tokens"], 1.5),
        _mutated(["usage"], KeyError),
        _mutated(["model"], 7),
        b'{"model":"m","answers":{"destructive":{"type":"noul","noul":' + b"9" * 400 + b'}},"usage":{"input_tokens":1,"output_tokens":1}}',
        b'{"model":"m","answers":{"destructive":{"type":"noul","noul":0.1}},"usage":{"input_tokens":' + b"9" * 400 + b',"output_tokens":1}}',
        _mutated(["usage", "input_tokens"], 2**31 + 1),
        json.dumps(DOCUMENTED).replace('"2": 0.82', '"2": ' + "9" * 400).encode(),
        json.dumps(DOCUMENTED).replace('"score": 1.78', '"score": -' + "9" * 400).encode(),
        b"[]",
        b"\"x\"",
        b"not json",
        b"\xff\xfe",
        b"",
        b"{" * 100000,
        b"x" * (64 * 1024 + 1),
        b'{"model":"m","answers":{"destructive":{"type":"noul","noul":0.1}},'
        b'"answers":{"destructive":{"type":"noul","noul":0.1}},"usage":{"input_tokens":1,"output_tokens":1}}',
        b'{"model":"m","answers":{"destructive":{"type":"noul","noul":0.1,"noul":0.2}},'
        b'"usage":{"input_tokens":1,"output_tokens":1}}',
    ],
)
def test_parse_rejects_hostile_or_malformed(body: bytes) -> None:
    with pytest.raises(JudgeUnavailable) as err:
        parse_response(body, EXPECTED, QUESTIONS)
    assert err.value.code in ("malformed", "too_large")
    assert str(err.value) == err.value.code


def test_parse_oversize_code_is_too_large() -> None:
    with pytest.raises(JudgeUnavailable) as err:
        parse_response(b" " * (64 * 1024 + 1), EXPECTED)
    assert err.value.code == "too_large"


def test_parse_ignores_extra_keys_inside_answers() -> None:
    doc = json.loads(json.dumps(DOCUMENTED))
    doc["answers"]["destructive"]["note"] = "ignore me"
    doc["extra_top"] = 1
    assert parse_response(_body(doc), EXPECTED, QUESTIONS).answers["destructive"] == NoulAnswer(0.12)


def test_parse_score_level_count_must_match_question() -> None:
    q = {**QUESTIONS, "severity": {"type": "score", "criteria": ["a", "b"]}}
    with pytest.raises(JudgeUnavailable):
        parse_response(_body(DOCUMENTED), EXPECTED, q)


# ------------------------------------------------------------ build_request


def test_build_request_shape_per_kind() -> None:
    body = build_request("laya-auto", {"tool": "Bash", "verb": "rm"}, QUESTIONS)
    doc = json.loads(body)
    assert doc["model"] == "laya-auto" and doc["state"] == {"tool": "Bash", "verb": "rm"}
    assert doc["questions"]["destructive"]["type"] == "noul"
    assert doc["questions"]["severity"]["criteria"] == ["low", "mid", "high"]
    assert doc["questions"]["bucket"]["criteria"]["billing"] == "pay"
    assert isinstance(body, bytes)


def test_build_request_rejections() -> None:
    with pytest.raises(ValueError):
        build_request("m", "s", {})
    with pytest.raises(ValueError):
        build_request("m", "x" * (256 * 1024), QUESTIONS)
    with pytest.raises(ValueError):
        build_request("m", "s", {"q": {"type": "score", "criteria": {"a": "b"}}})
    with pytest.raises(ValueError):
        build_request("m", "s", {"q": {"type": "score", "criteria": []}})
    with pytest.raises(ValueError):
        build_request("m", "s", {"q": {"type": "choice", "criteria": ["a"]}})
    with pytest.raises(ValueError):
        build_request("m", "s", {"q": {"type": "other"}})
    with pytest.raises(ValueError):
        build_request("m", float("nan"), QUESTIONS)


# ------------------------------------------------------------ client config failures


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    p = tmp_path / "jev.key"
    p.write_text("sk-test-not-a-real-key\n")
    p.chmod(0o600)
    return p


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.parametrize("endpoint", ["ftp://x/", "not a url", "http://", "", "http://u:p@127.0.0.1:1/", "http://127.0.0.1:99999/"])
def test_bad_endpoint_is_config(endpoint: str, key_file: Path) -> None:
    with pytest.raises(JudgeUnavailable) as err:
        JevClient(endpoint, str(key_file), "laya-auto", 1.0)
    assert err.value.code == "config"


def test_unreachable_port_is_connect(key_file: Path) -> None:
    c = JevClient(f"http://127.0.0.1:{_closed_port()}", str(key_file), "laya-auto", 2.0)
    with pytest.raises(JudgeUnavailable) as err:
        c.ask("s", QUESTIONS)
    assert err.value.code == "connect"
    with pytest.raises(JudgeUnavailable) as err2:
        c.health()
    assert err2.value.code == "connect"


def test_key_file_checks(tmp_path: Path, key_file: Path) -> None:
    url = f"http://127.0.0.1:{_closed_port()}"

    def code(path: str) -> str:
        with pytest.raises(JudgeUnavailable) as err:
            JevClient(url, path, "laya-auto", 1.0).health()
        return err.value.code

    assert code(str(tmp_path / "missing")) == "config"
    wide = tmp_path / "wide.key"
    wide.write_text("k")
    wide.chmod(0o640)
    assert code(str(wide)) == "config"
    link = tmp_path / "link.key"
    link.symlink_to(key_file)
    assert code(str(link)) == "config"
    empty = tmp_path / "empty.key"
    empty.write_text("\n")
    empty.chmod(0o600)
    assert code(str(empty)) == "config"
    assert code(str(tmp_path)) == "config"
    badchars = tmp_path / "bad.key"
    badchars.write_text("abc\r\nX-Evil: 1")
    badchars.chmod(0o600)
    assert code(str(badchars)) == "config"
    if os.geteuid() == 0 or not Path("/etc/shadow").exists():
        pytest.skip("wrong-owner check needs a non-root user and a root-owned regular file")
    assert code("/etc/hostname") == "config"  # regular file owned by root, not by us


def test_key_never_in_repr_or_error(key_file: Path) -> None:
    c = JevClient(f"http://127.0.0.1:{_closed_port()}", str(key_file), "laya-auto", 1.0)
    with pytest.raises(JudgeUnavailable) as err:
        c.ask("s", QUESTIONS)
    assert "sk-test" not in repr(c) and "sk-test" not in str(err.value) and "sk-test" not in repr(err.value)


# ------------------------------------------------------------ hostile listeners


class Listener:
    """Raw TCP listener that produces hostile transport behavior for one connection at a time."""

    def __init__(self, behavior: Callable[[socket.socket], None], read_full: bool = False) -> None:
        self._read_full = read_full
        self._lock = threading.Lock()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = int(self._srv.getsockname()[1])
        self._behavior = behavior
        self._stop = threading.Event()
        self.seen = b""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._srv.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            try:
                data = self._read_request(conn) if self._read_full else conn.recv(65536)
                with self._lock:
                    self.seen += data
                self._behavior(conn)
            except OSError:
                pass

    @staticmethod
    def _read_request(conn: socket.socket, cap: int = 1 << 20) -> bytes:
        """Read headers up to the blank line, then Content-Length body bytes (conn timeout applies)."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk or len(buf) > cap:
                return buf
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = min(int(value.strip() or b"0"), cap)
        while len(body) < length:
            chunk = conn.recv(65536)
            if not chunk:
                break
            body += chunk
        return head + b"\r\n\r\n" + body

    def close(self) -> None:
        self._stop.set()
        self._srv.close()


@pytest.fixture
def listener() -> Iterator[Callable[..., Listener]]:
    made: list[Listener] = []

    def make(behavior: Callable[[socket.socket], None], read_full: bool = False) -> Listener:
        li = Listener(behavior, read_full)
        made.append(li)
        return li

    yield make
    for li in made:
        li.close()


def _client(li: Listener, key_file: Path, timeout: float = 1.0) -> JevClient:
    return JevClient(f"http://127.0.0.1:{li.port}", str(key_file), "laya-auto", timeout)


def test_silent_server_times_out_within_budget(listener: Any, key_file: Path) -> None:
    li = listener(lambda c: time.sleep(4))
    t0 = time.monotonic()
    with pytest.raises(JudgeUnavailable) as err:
        _client(li, key_file, 1.0).ask("s", QUESTIONS)
    assert err.value.code == "timeout"
    assert time.monotonic() - t0 < 1.5
    assert b"Bearer sk-test-not-a-real-key" in li.seen and b"herdr-assistant-judge/1" in li.seen


def test_redirect_is_not_followed(listener: Any, key_file: Path) -> None:
    def redirect(c: socket.socket) -> None:
        c.sendall(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/evil\r\nContent-Length: 0\r\n\r\n")

    li = listener(redirect)
    with pytest.raises(JudgeUnavailable) as err:
        _client(li, key_file).ask("s", QUESTIONS)
    assert err.value.code == "http_302"


@pytest.mark.parametrize("status,code", [(401, "auth"), (403, "auth"), (422, "http_422"), (429, "http_429"), (503, "http_503")])
def test_error_statuses_map_to_codes_without_body(listener: Any, key_file: Path, status: int, code: str) -> None:
    secret = b'{"error":{"message":"LEAKME-body-text"}}'

    def reply(c: socket.socket) -> None:
        c.sendall(b"HTTP/1.1 %d X\r\nContent-Length: %d\r\n\r\n" % (status, len(secret)) + secret)

    li = listener(reply)
    with pytest.raises(JudgeUnavailable) as err:
        _client(li, key_file).ask("s", QUESTIONS)
    assert err.value.code == code and "LEAKME" not in str(err.value) + repr(err.value)


def test_oversize_stream_is_too_large(listener: Any, key_file: Path) -> None:
    def flood(c: socket.socket) -> None:
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
        for _ in range(64):
            c.sendall(b"a" * 4096)

    li = listener(flood)
    with pytest.raises(JudgeUnavailable) as err:
        _client(li, key_file, 3.0).ask("s", QUESTIONS)
    assert err.value.code == "too_large"


def test_declared_oversize_content_length_is_too_large(listener: Any, key_file: Path) -> None:
    li = listener(lambda c: c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 999999\r\n\r\n"))
    with pytest.raises(JudgeUnavailable) as err:
        _client(li, key_file, 3.0).ask("s", QUESTIONS)
    assert err.value.code == "too_large"


def test_slow_drip_body_bounded_by_deadline(listener: Any, key_file: Path) -> None:
    def drip(c: socket.socket) -> None:
        c.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        for _ in range(100):
            c.sendall(b" ")
            time.sleep(0.2)

    li = listener(drip)
    t0 = time.monotonic()
    with pytest.raises(JudgeUnavailable) as err:
        _client(li, key_file, 1.0).ask("s", QUESTIONS)
    assert err.value.code == "timeout"
    assert time.monotonic() - t0 < 1.7


def test_slow_drip_headers_bounded_by_deadline(listener: Any, key_file: Path) -> None:
    def drip(c: socket.socket) -> None:
        for b in b"HTTP/1.1 200 OK\r\nX-A: " + b"a" * 200:
            c.sendall(bytes([b]))
            time.sleep(0.2)

    li = listener(drip)
    t0 = time.monotonic()
    with pytest.raises(JudgeUnavailable) as err:
        _client(li, key_file, 1.0).ask("s", QUESTIONS)
    assert err.value.code == "timeout"
    assert time.monotonic() - t0 < 1.7


def test_per_call_timeout_override(listener: Any, key_file: Path) -> None:
    li = listener(lambda c: time.sleep(4))
    t0 = time.monotonic()
    with pytest.raises(JudgeUnavailable):
        _client(li, key_file, 30.0).ask("s", QUESTIONS, timeout=0.5)
    assert time.monotonic() - t0 < 1.0


def test_garbage_http_is_failure_not_crash(listener: Any, key_file: Path) -> None:
    li = listener(lambda c: c.sendall(b"\x00\x01garbage not http\r\n\r\n"))
    with pytest.raises(JudgeUnavailable):
        _client(li, key_file).ask("s", QUESTIONS)


def test_oversize_request_is_config(listener: Any, key_file: Path) -> None:
    li = listener(lambda c: None)
    with pytest.raises(JudgeUnavailable) as err:
        _client(li, key_file).ask("x" * (300 * 1024), QUESTIONS)
    assert err.value.code == "config"


def test_tls_failure_against_plain_listener_is_connect(listener: Any, key_file: Path) -> None:
    li = listener(lambda c: c.sendall(b"not tls at all"))
    c = JevClient(f"https://127.0.0.1:{li.port}", str(key_file), "laya-auto", 2.0)
    with pytest.raises(JudgeUnavailable) as err:
        c.ask("s", QUESTIONS)
    assert err.value.code == "connect"


# ------------------------------------------------------------ integration (real System One)

_URL = os.environ.get("HERDR_JEV_TEST_URL", "")
_KEY = os.environ.get("HERDR_JEV_TEST_KEY_FILE", "")
_model = os.environ.get("HERDR_JEV_TEST_MODEL", "laya-auto")


@pytest.mark.integration
@pytest.mark.skipif(not (_URL and _KEY), reason="set HERDR_JEV_TEST_URL and HERDR_JEV_TEST_KEY_FILE to a real System One (mock backend ok)")
def test_integration_health_and_ask() -> None:
    client = JevClient(_URL, _KEY, _model, 30.0)
    assert _model in client.health()
    res = client.ask({"tool": "Bash", "verb": "ls"}, QUESTIONS)
    assert isinstance(res, JudgeResult) and res.model == _model
    assert set(res.answers) == set(QUESTIONS)
    assert isinstance(res.answers["destructive"], NoulAnswer)
    assert isinstance(res.answers["severity"], ScoreAnswer)
    assert isinstance(res.answers["bucket"], ChoiceAnswer)


# ------------------------------------------------------------ review fixes


def test_parse_rejects_empty_expected() -> None:
    with pytest.raises(JudgeUnavailable) as err:
        parse_response(b'{"model":"m","answers":{},"usage":{"input_tokens":1,"output_tokens":1}}', {})
    assert err.value.code == "malformed"


@pytest.mark.parametrize("t", [0, -1, float("nan"), float("inf"), 601])
def test_ask_timeout_validated(t: float, key_file: Path) -> None:
    c = JevClient(f"http://127.0.0.1:{_closed_port()}", str(key_file), "laya-auto", 1.0)
    with pytest.raises(JudgeUnavailable) as err:
        c.ask("s", QUESTIONS, timeout=t)
    assert err.value.code == "config"


def _ok_http(body: bytes, rid: str = "") -> bytes:
    extra = f"X-InferWeave-Request-ID: {rid}\r\n" if rid else ""
    return b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + extra.encode() + b"Content-Length: %d\r\n\r\n" % len(body) + body


def test_ask_success_over_wire_resolved_model_and_request_id(listener: Any, key_file: Path) -> None:
    """Success-path body test: the DOCUMENTED sample (not captured) served over a raw socket."""
    doc = json.loads(json.dumps(DOCUMENTED))
    doc["model"] = "concrete-checkpoint-id"
    li = listener(lambda c: c.sendall(_ok_http(_body(doc), "req-42")), read_full=True)
    res = _client(li, key_file, 3.0).ask({"tool": "Bash"}, QUESTIONS)
    assert res.model == "concrete-checkpoint-id" and res.request_id == "req-42"
    assert b"POST /v1/systemone" in li.seen and b'"model":"laya-auto"' in li.seen


def test_health_success_over_wire(listener: Any, key_file: Path) -> None:
    """Success-path: an OpenAI-shaped model list served over a raw socket."""
    li = listener(lambda c: c.sendall(_ok_http(b'{"object":"list","data":[{"id":"laya-auto"},{"id":"b"}]}')), read_full=True)
    assert _client(li, key_file, 3.0).health() == ["laya-auto", "b"]
    assert b"GET /v1/models" in li.seen


@pytest.mark.parametrize(
    "body",
    [b"x" * (64 * 1024 + 1), b'{"data":"nope"}', b'{"data":[{"id":1}]}', b"[]", b'{"data":[{"id":"a"}],"data":[]}', b'{"data":[{"id":"a","id":"b"}]}'],
)
def test_health_hostile_bodies(listener: Any, key_file: Path, body: bytes) -> None:
    li = listener(lambda c: c.sendall(_ok_http(body)))
    with pytest.raises(JudgeUnavailable) as err:
        _client(li, key_file, 3.0).health()
    assert err.value.code in ("malformed", "too_large")


def test_key_rotation_between_calls(listener: Any, key_file: Path) -> None:
    li = listener(lambda c: c.sendall(_ok_http(b'{"data":[{"id":"m"}]}')), read_full=True)
    client = _client(li, key_file, 3.0)
    client.health()
    assert b"Bearer sk-test-not-a-real-key" in li.seen
    key_file.write_text("sk-rotated-key\n")
    li.seen = b""
    client.health()
    assert b"Bearer sk-rotated-key" in li.seen and b"sk-test-not-a-real-key" not in li.seen


def test_slow_connect_bounded_by_deadline(key_file: Path) -> None:
    """Full-backlog listener makes further SYNs hang; connect must still respect the budget."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(0)
    port = int(srv.getsockname()[1])
    fillers = []
    try:
        for _ in range(8):
            f = socket.socket()
            f.setblocking(False)
            f.connect_ex(("127.0.0.1", port))
            fillers.append(f)
        probe = socket.socket()
        probe.settimeout(0.3)
        try:
            probe.connect(("127.0.0.1", port))
            pytest.skip("kernel accepted the probe connection; cannot build a hanging connect here")
        except OSError:
            pass
        finally:
            probe.close()
        c = JevClient(f"http://127.0.0.1:{port}", str(key_file), "laya-auto", 1.0)
        t0 = time.monotonic()
        with pytest.raises(JudgeUnavailable) as err:
            c.health()
        assert err.value.code in ("timeout", "connect") and time.monotonic() - t0 < 1.5
    finally:
        for f in fillers:
            f.close()
        srv.close()
