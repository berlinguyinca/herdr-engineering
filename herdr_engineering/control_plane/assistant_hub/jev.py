"""Jev (InferWeave System One) judge client: request builder, strict response validation, transport.

Everything here fails closed: any deviation from the documented contract raises
``JudgeUnavailable`` with a short code.  Response bodies (including error envelopes) and the
API key are never copied into exceptions, ``repr`` or logs.

Transport notes: one connection per call (no pooling in v1; the 5 s authorize budget and a
single worker make it unnecessary).  The whole call is bounded by a monotonic deadline: connect
and every read use the remaining budget, and a watchdog timer shuts the socket down at the
deadline so a slow-drip response cannot outlive ``timeout``.  Redirects are never followed.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import socket
import ssl
import stat
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CHOICES = 256
USER_AGENT = "herdr-assistant-judge/1"
_SUM_TOLERANCE = 0.05
_KINDS = ("noul", "score", "choice")


class JudgeUnavailable(Exception):
    """The judge could not give a trustworthy answer.  ``str()`` is only the short code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class NoulAnswer:
    p_yes: float


@dataclass(frozen=True)
class ScoreAnswer:
    expected: float
    confidence: float
    probabilities: Mapping[str, float]


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: Mapping[str, float]


Answer = NoulAnswer | ScoreAnswer | ChoiceAnswer


@dataclass(frozen=True)
class JudgeResult:
    model: str
    answers: Mapping[str, Answer]
    input_tokens: int
    output_tokens: int
    request_id: str


# ---------------------------------------------------------------- request


def build_request(model: str, state: object, questions: Mapping[str, Mapping[str, Any]]) -> bytes:
    """Serialize a System One request.  ``score.criteria`` is a list, ``choice.criteria`` a map."""
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")
    if not questions:
        raise ValueError("questions must not be empty")
    for name, q in questions.items():
        if not isinstance(name, str) or not name:
            raise ValueError("question names must be non-empty strings")
        _check_question(name, q)
    payload = {"state": state, "model": model, "questions": {n: dict(q) for n, q in questions.items()}}
    try:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("request is not JSON serializable") from exc
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds 256 KiB")
    return body


def _check_question(name: str, q: Mapping[str, Any]) -> None:
    kind = q.get("type")
    if kind not in _KINDS:
        raise ValueError(f"question {name!r}: unknown type")
    crit = q.get("criteria")
    if kind == "score" and not (isinstance(crit, list) and crit):
        raise ValueError(f"question {name!r}: score criteria must be a non-empty list")
    if kind == "choice" and not (isinstance(crit, dict) and 0 < len(crit) <= MAX_CHOICES):
        raise ValueError(f"question {name!r}: choice criteria must be a map of 1..{MAX_CHOICES}")


# ---------------------------------------------------------------- response


def _malformed() -> JudgeUnavailable:
    return JudgeUnavailable("malformed")


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _num(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _malformed()
    if isinstance(v, int) and abs(v) > 2**53:
        raise _malformed()
    f = float(v)
    if not math.isfinite(f):
        raise _malformed()
    return f


def _prob(v: Any) -> float:
    f = _num(v)
    if not 0.0 <= f <= 1.0:
        raise _malformed()
    return f


def _probabilities(raw: Any, keys: list[str] | None) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise _malformed()
    out = {str(k): _prob(v) for k, v in raw.items()}
    if keys is not None and set(out) != set(keys):
        raise _malformed()
    if abs(sum(out.values()) - 1.0) > _SUM_TOLERANCE:
        raise _malformed()
    return out


def _count(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 2**31:
        raise _malformed()
    return v


def parse_response(
    body: bytes,
    expected: Mapping[str, str],
    questions: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    request_id: str = "",
) -> JudgeResult:
    """Validate a System One response.  ``expected`` maps question name -> kind.

    When ``questions`` is given, score probability keys must be exactly ``0..len(criteria)-1`` and
    choice probability keys exactly the criteria names; otherwise score keys must be a contiguous
    ``0..n-1`` range and choice probabilities must contain the chosen name.
    """
    if not expected:
        raise _malformed()
    if len(body) > MAX_RESPONSE_BYTES:
        raise JudgeUnavailable("too_large")
    try:
        doc = json.loads(body.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _malformed() from exc
    if not isinstance(doc, dict):
        raise _malformed()
    model = doc.get("model")
    answers_raw = doc.get("answers")
    usage = doc.get("usage")
    if not isinstance(model, str) or not model or not isinstance(answers_raw, dict) or not isinstance(usage, dict):
        raise _malformed()
    if set(answers_raw) != set(expected):
        raise _malformed()
    answers: dict[str, Answer] = {}
    for name, kind in expected.items():
        raw = answers_raw[name]
        if not isinstance(raw, dict) or raw.get("type") != kind:
            raise _malformed()
        crit = (questions or {}).get(name, {}).get("criteria") if questions else None
        answers[name] = _parse_answer(kind, raw, crit)
    rid = "".join(c for c in request_id[:64] if c.isprintable() and not c.isspace())
    return JudgeResult(
        model=model,
        answers=answers,
        input_tokens=_count(usage.get("input_tokens")),
        output_tokens=_count(usage.get("output_tokens")),
        request_id=rid,
    )


def _parse_answer(kind: str, raw: dict[str, Any], crit: Any) -> Answer:
    if kind == "noul":
        return NoulAnswer(p_yes=_prob(raw.get("noul")))
    if kind == "score":
        probs = _probabilities(raw.get("probabilities"), [str(i) for i in range(len(crit))] if isinstance(crit, list) else None)
        if crit is None and set(probs) != {str(i) for i in range(len(probs))}:
            raise _malformed()
        expected_level = _num(raw.get("score"))
        if not 0.0 <= expected_level <= len(probs) - 1:
            raise _malformed()
        return ScoreAnswer(expected_level, _prob(raw.get("confidence")), probs)
    choice = raw.get("choice")
    if not isinstance(choice, str):
        raise _malformed()
    probs = _probabilities(raw.get("probabilities"), list(crit) if isinstance(crit, dict) else None)
    if choice not in probs:
        raise _malformed()
    return ChoiceAnswer(choice, _prob(raw.get("confidence")), probs)


# ---------------------------------------------------------------- transport


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self.end - time.monotonic())

    def expired(self) -> bool:
        return time.monotonic() >= self.end


class JevClient:
    """Stateless client; the key file is re-read on every call so rotation takes effect at once."""

    def __init__(self, endpoint: str, api_key_file: str, model: str, timeout_seconds: float) -> None:
        self._model = model
        self._key_file = api_key_file
        self._timeout = float(timeout_seconds)
        self._scheme, self._host, self._port, self._base = _parse_endpoint(endpoint)
        if not (0 < self._timeout <= 600) or not model:
            raise JudgeUnavailable("config")

    def __repr__(self) -> str:
        return f"JevClient({self._scheme}://{self._host}:{self._port}, model={self._model!r})"

    def health(self) -> list[str]:
        status, body, _ = self._call("GET", "/v1/models", None, self._timeout)
        _raise_for_status(status)
        try:
            doc = json.loads(body.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_reject_constant)
            data = doc["data"]
            ids = [item["id"] for item in data]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError, RecursionError) as exc:
            raise _malformed() from exc
        if not isinstance(data, list) or not all(isinstance(i, str) and i for i in ids):
            raise _malformed()
        return ids

    def ask(self, state: object, questions: Mapping[str, Mapping[str, Any]], *, timeout: float | None = None) -> JudgeResult:
        eff = self._timeout if timeout is None else timeout
        if isinstance(eff, bool) or not isinstance(eff, (int, float)) or not math.isfinite(eff) or not 0 < eff <= 600:
            raise JudgeUnavailable("config")
        try:
            req = build_request(self._model, state, questions)
        except ValueError as exc:
            raise JudgeUnavailable("config") from exc
        status, body, rid = self._call("POST", "/v1/systemone", req, float(eff))
        _raise_for_status(status)
        expected = {n: str(q["type"]) for n, q in questions.items()}
        # The response model is the resolved checkpoint id (e.g. laya-auto -> concrete id); it is
        # kept verbatim in the result and only required to be a non-empty string.
        return parse_response(body, expected, questions, request_id=rid)

    # -- internals

    def _read_key(self) -> str:
        path = self._key_file
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise JudgeUnavailable("config")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fst = os.fstat(fd)
                if not stat.S_ISREG(fst.st_mode) or fst.st_uid != os.geteuid() or fst.st_mode & 0o077:
                    raise JudgeUnavailable("config")
                raw = os.read(fd, 8192)
            finally:
                os.close(fd)
            key = raw.decode("ascii").strip()
        except JudgeUnavailable:
            raise
        except (OSError, ValueError) as exc:
            raise JudgeUnavailable("config") from exc
        if not key or not key.isprintable() or " " in key:
            raise JudgeUnavailable("config")
        return key

    def _connect(self, conn: http.client.HTTPConnection, deadline: _Deadline) -> None:
        """Resolve, then try each address against the shared deadline (never a per-address timeout)."""
        infos = socket.getaddrinfo(self._host, self._port, type=socket.SOCK_STREAM)
        last: OSError = OSError("no address")
        for family, stype, proto, _, addr in infos:
            if deadline.expired():
                raise TimeoutError("deadline")
            sock = socket.socket(family, stype, proto)
            try:
                sock.settimeout(max(deadline.remaining(), 0.001))
                sock.connect(addr)
                if self._scheme == "https":
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self._host)
                conn.sock = sock
                return
            except TimeoutError:
                sock.close()
                raise
            except OSError as exc:
                sock.close()
                last = exc
        raise last

    def _call(self, method: str, path: str, body: bytes | None, timeout: float) -> tuple[int, bytes, str]:
        key = self._read_key()
        deadline = _Deadline(timeout)
        conn: http.client.HTTPConnection = (
            http.client.HTTPSConnection(self._host, self._port) if self._scheme == "https"
            else http.client.HTTPConnection(self._host, self._port)
        )
        fired = threading.Event()

        def _kill() -> None:
            fired.set()
            sock = conn.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        watchdog = threading.Timer(timeout, _kill)
        watchdog.daemon = True
        watchdog.start()
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": USER_AGENT}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            try:
                self._connect(conn, deadline)
            except ssl.SSLError as exc:
                raise JudgeUnavailable("connect") from exc
            except TimeoutError as exc:
                raise JudgeUnavailable("timeout") from exc
            except OSError as exc:
                raise JudgeUnavailable("timeout" if fired.is_set() else "connect") from exc
            _arm(conn, deadline)
            conn.request(method, self._base + path, body=body, headers=headers)
            resp = conn.getresponse()
            rid = resp.getheader("X-InferWeave-Request-ID") or ""
            if resp.status != 200:
                return resp.status, b"", rid
            length = resp.getheader("Content-Length")
            if length is not None and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                raise JudgeUnavailable("too_large")
            chunks: list[bytes] = []
            total = 0
            while True:
                if deadline.expired():
                    raise JudgeUnavailable("timeout")
                _arm(conn, deadline)
                chunk = resp.read1(min(4096, MAX_RESPONSE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise JudgeUnavailable("too_large")
            return resp.status, b"".join(chunks), rid
        except JudgeUnavailable:
            raise
        except TimeoutError as exc:
            raise JudgeUnavailable("timeout") from exc
        except (http.client.HTTPException, OSError) as exc:
            raise JudgeUnavailable("timeout" if fired.is_set() or deadline.expired() else "malformed") from exc
        finally:
            watchdog.cancel()
            conn.close()


def _arm(conn: http.client.HTTPConnection, deadline: _Deadline) -> None:
    sock = conn.sock
    if sock is not None:
        sock.settimeout(max(deadline.remaining(), 0.001))


def _raise_for_status(status: int) -> None:
    if status == 200:
        return
    if status in (401, 403):
        raise JudgeUnavailable("auth")
    raise JudgeUnavailable(f"http_{status}")


def _parse_endpoint(endpoint: str) -> tuple[str, str, int, str]:
    try:
        parts = urlsplit(endpoint)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise JudgeUnavailable("config") from exc
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password or parts.query or parts.fragment:
        raise JudgeUnavailable("config")
    base = parts.path.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return parts.scheme, host, port or (443 if parts.scheme == "https" else 80), base
