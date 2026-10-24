"""Reviewer worker for the review gate: claim a job, call the model, post the text.

The worker is the only review-gate component that talks to an LLM. It reads
attacker-influenced prompt text (from the PR) and returns model output to the
control plane, so it is built to hold nothing worth stealing and to leak nothing:

* It holds no GitHub credential. Its only secrets are the control plane's
  ``reviewer`` role key and, optionally, a model API key. Both live in 0600 key
  files that are re-read on every request, so rotation needs no restart.
* Logs carry only a job id (32 hex) and a fixed outcome word. Failure reasons
  sent to ``/failure`` are a fixed word or an exception class name. Response
  bodies, URLs, headers, prompt text and model output never reach a log line, a
  reason or an exception message.
* Every request has a total wall-clock deadline (its timeout covers the whole
  exchange, not one socket read, so a dripping endpoint cannot hold a lease
  through heartbeats), every response is read to at most 1 MiB, no redirect is
  followed, and no proxy is taken from the environment. A model call past
  ``model.timeout_seconds`` is abandoned and reported as ``model_timeout``.
* Claims are protected by the ``claim_token`` the control plane returns with a
  job; it is sent with every heartbeat, result and failure and never logged.
* Configuration comes only from the YAML file (no environment variables).
* Failed attempts are spaced out on purpose. A ``/failure`` releases the job
  straight back to the queue and the PR escalates after ``max_attempts``, so a
  worker whose model is down must not re-claim at once and burn every attempt
  in milliseconds. After each job that does not end in a recorded (or stale)
  result the worker waits ``poll_seconds``, doubling per consecutive failure up
  to ``MAX_BACKOFF_SECONDS``; a recorded result or an idle claim resets it.

The control plane authenticates the key with the ``X-API-Key`` header (as every
other control-plane client does); only the model call uses
``Authorization: Bearer``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

import yaml

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_BACKOFF_SECONDS = 300
CONTROL_PLANE_TIMEOUT = 30
_MAX_KEY_BYTES = 64 * 1024
# Same pattern the server applies to worker_id / reviewer_model (control_plane/api.py).
_ID = re.compile(r"[A-Za-z0-9._:/-]{1,128}")
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_MAX_LEASE_SECONDS = 86400
_MESSAGE_ROLES = frozenset({"system", "user", "assistant"})
_CLAIM_FIELDS = frozenset(
    {"job_id", "repo", "number", "sha", "messages", "lease_seconds", "attempt", "claim_token"}
)
_CLAIM_TOKEN = re.compile(r"[0-9a-f]{64}")
_READ_CHUNK = 64 * 1024
_RESULT_OUTCOMES = frozenset({"recorded", "stale", "rejected"})
_FAILURE_OUTCOMES = frozenset({"released", "escalated", "stale"})
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_TOP_KEYS = frozenset({"control_plane_url", "api_key_file", "worker_id", "poll_seconds", "model"})
_MODEL_KEYS = frozenset({"base_url", "model", "api_key_file", "max_tokens", "timeout_seconds"})
_CLAIM_PATH = "/api/v1/review-gate/claim"
_JOBS_PATH = "/api/v1/review-gate/jobs/"

_log = logging.getLogger(__name__)


class WorkerConfigError(ValueError):
    """A config or key-file problem. The message is fixed text: no file contents."""


class WorkerAuthError(Exception):
    """The control plane refused the reviewer key (401/403). Stops the worker."""


class _DeadlineExceeded(TimeoutError):
    """The whole request (not one socket operation) ran past its timeout."""


class _ModelError(Exception):
    """The model endpoint answered with a non-200 status."""


class _BadResponse(Exception):
    """A response that is not exactly the expected JSON shape."""


class _TooLarge(Exception):
    """A response over MAX_RESPONSE_BYTES."""


class _Truncated(Exception):
    """The model stopped at max_tokens (finish_reason "length")."""


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    api_key_file: str | None
    max_tokens: int = 4096
    timeout_seconds: int = 300


@dataclass(frozen=True)
class WorkerConfig:
    control_plane_url: str
    api_key_file: str
    worker_id: str
    model: ModelConfig
    poll_seconds: int = 10


# ------------------------------------------------------------------ key files
def read_key(path: str) -> str:
    """Return the token in ``path`` (whitespace stripped), read fresh on every call.

    The file must be an absolute path to a regular file (symlinks are followed)
    with mode exactly 0600 and must hold one printable token. The checks run on
    the opened descriptor, so a swap between check and read is impossible.
    Errors are fixed text and never contain the file contents.
    """
    if not isinstance(path, str) or not os.path.isabs(path):
        raise WorkerConfigError("key file must be an absolute path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        raise WorkerConfigError("key file must exist and be mode 0600") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise WorkerConfigError("key file must be a regular file")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise WorkerConfigError("key file must be mode 0600")
        data = os.read(fd, _MAX_KEY_BYTES + 1)
    except OSError:
        raise WorkerConfigError("key file is unreadable") from None
    finally:
        os.close(fd)
    if len(data) > _MAX_KEY_BYTES:
        raise WorkerConfigError("key file is too large")
    try:
        key = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise WorkerConfigError("key file must hold one printable ASCII token") from None
    if not key:
        raise WorkerConfigError("key file is empty")
    if not all(33 <= ord(c) <= 126 for c in key):
        raise WorkerConfigError("key file must hold one printable ASCII token")
    return key


# --------------------------------------------------------------------- config
def _check_url(value, where: str) -> str:
    """An http(s) URL with a host, no credentials, query or fragment.

    Plain http is accepted only for a loopback host (127.0.0.1, ::1, localhost);
    every other host, a private-network lab host included, needs https. The
    operator puts a TLS front in place rather than this check trusting a LAN.
    Errors never echo the URL (it could embed a credential).
    """
    if not isinstance(value, str) or not value:
        raise WorkerConfigError(f"{where} must be an http(s) URL")
    if not all(33 <= ord(c) <= 126 for c in value):
        raise WorkerConfigError(f"{where} must be an http(s) URL without spaces")
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise WorkerConfigError(f"{where} must be an http(s) URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WorkerConfigError(f"{where} must be an http(s) URL with a host")
    if "@" in parts.netloc:
        raise WorkerConfigError(f"{where} must not embed credentials")
    if parts.query or parts.fragment or "?" in value or "#" in value:
        raise WorkerConfigError(f"{where} must not have a query or fragment")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise WorkerConfigError(f"{where}: plain http is allowed only for a loopback host")
    return value.rstrip("/")


def _mapping(raw, where):
    if not isinstance(raw, dict):
        raise WorkerConfigError(f"{where} must be a mapping")
    for key in raw:
        if not isinstance(key, str):
            raise WorkerConfigError(f"{where}: keys must be strings")
    return raw


def _keys(raw, allowed, required, where):
    if set(raw) - allowed:
        # Never echo the key itself: a secret pasted as a key would be printed.
        raise WorkerConfigError(f"{where}: unknown key")
    for key in sorted(required):
        if key not in raw:
            raise WorkerConfigError(f"{where}: {key} is required")


def _int(raw, key, where, default, minimum, maximum):
    if key not in raw:
        return default
    val = raw[key]
    if isinstance(val, bool) or not isinstance(val, int) or not minimum <= val <= maximum:
        raise WorkerConfigError(f"{where}: {key} must be an integer in [{minimum}, {maximum}]")
    return val


def _ident(raw, key, where):
    val = raw[key]
    if not isinstance(val, str) or not _ID.fullmatch(val):
        raise WorkerConfigError(f"{where}: {key} must match [A-Za-z0-9._:/-]{{1,128}}")
    return val


def _key_file(value, where):
    if not isinstance(value, str) or not os.path.isabs(value):
        raise WorkerConfigError(f"{where} must be an absolute path")
    try:
        read_key(value)
    except WorkerConfigError as exc:
        raise WorkerConfigError(f"{where}: {exc}") from None
    return value


def load_worker_config(path: str) -> WorkerConfig:
    """Read and validate the worker YAML; raises ``WorkerConfigError`` (a ValueError)."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except (OSError, UnicodeDecodeError):
        raise WorkerConfigError("worker config: cannot read the file") from None
    except yaml.YAMLError:
        raise WorkerConfigError("worker config: not valid YAML") from None
    raw = _mapping(raw, "worker config")
    _keys(raw, _TOP_KEYS, _TOP_KEYS - {"poll_seconds"}, "worker config")
    model_raw = _mapping(raw["model"], "worker config: model")
    _keys(model_raw, _MODEL_KEYS, {"base_url", "model"}, "worker config: model")
    model_key = model_raw.get("api_key_file")
    model = ModelConfig(
        base_url=_check_url(model_raw["base_url"], "model.base_url"),
        model=_ident(model_raw, "model", "worker config: model"),
        api_key_file=None if model_key is None else _key_file(model_key, "model.api_key_file"),
        max_tokens=_int(model_raw, "max_tokens", "worker config: model", 4096, 1, 1_000_000),
        timeout_seconds=_int(model_raw, "timeout_seconds", "worker config: model", 300, 1, 3600),
    )
    return WorkerConfig(
        control_plane_url=_check_url(raw["control_plane_url"], "control_plane_url"),
        api_key_file=_key_file(raw["api_key_file"], "api_key_file"),
        worker_id=_ident(raw, "worker_id", "worker config"),
        model=model,
        poll_seconds=_int(raw, "poll_seconds", "worker config", 10, 1, 3600),
    )


# ------------------------------------------------------------------ transport
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the 3xx surfaces as an ordinary non-200 status."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ProxyHandler({}) keeps http_proxy/https_proxy from the environment out of the path.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _urllib_http(method, url, headers, body, timeout, *, clock=time.monotonic):
    """POST via urllib: ``(status, headers, body)``; body read to MAX_RESPONSE_BYTES + 1.

    ``timeout`` is both the per-socket-operation timeout and a TOTAL deadline:
    the body is read in chunks and ``clock`` (monotonic seconds) is checked
    between reads, raising ``_DeadlineExceeded`` (a TimeoutError) once past it.
    Error-status bodies are discarded unread; only the status and headers remain.
    Transport problems raise (the caller reports the exception class name only).
    """
    deadline = clock() + timeout
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        status, hdrs = exc.code, dict(exc.headers.items()) if exc.headers else {}
        exc.close()
        return status, hdrs, b""
    with resp:
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_RESPONSE_BYTES:
            if clock() >= deadline:
                raise _DeadlineExceeded()
            chunk = resp.read1(min(_READ_CHUNK, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return resp.status, dict(resp.headers.items()), b"".join(chunks)


def _header(headers, name):
    for key, value in (headers or {}).items():
        if isinstance(key, str) and key.lower() == name.lower():
            return value
    return None


def _retry_after(headers, default):
    value = _header(headers, "Retry-After")
    if isinstance(value, str):
        value = value.strip()
        if value.isascii() and value.isdigit() and len(value) <= 6 and int(value) >= 1:
            return min(int(value), MAX_BACKOFF_SECONDS)
    return default


def _reject_constant(_name):
    raise ValueError("non-finite number")


def _json_object(raw) -> dict:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise _TooLarge()
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise _BadResponse() from None
    if not isinstance(value, dict):
        raise _BadResponse()
    return value


def _token_ok(token) -> bool:
    return isinstance(token, str) and _CLAIM_TOKEN.fullmatch(token) is not None


def _valid_job(job) -> bool:
    if set(job) != _CLAIM_FIELDS:
        return False
    if not _token_ok(job.get("claim_token")):
        return False
    messages = job["messages"]
    if not isinstance(messages, list) or not messages:
        return False
    for m in messages:
        if not isinstance(m, dict) or set(m) != {"role", "content"}:
            return False
        if m["role"] not in _MESSAGE_ROLES or not isinstance(m["content"], str):
            return False
    lease = job["lease_seconds"]
    return (
        isinstance(lease, int) and not isinstance(lease, bool) and 1 <= lease <= _MAX_LEASE_SECONDS
    )


# --------------------------------------------------------------------- worker
class ReviewWorker:
    """Claims review jobs from the control plane and answers them with one model call.

    ``http`` is the transport seam: ``http(method, url, headers, body, timeout)``
    returning ``(status, headers, body_bytes)``. ``sleep`` is accepted for
    interface compatibility; waits use ``threading.Event.wait`` so ``run(stop)``
    and the heartbeat thread stop promptly. ``clock`` (monotonic seconds) times
    the total model-call deadline.
    """

    def __init__(self, config: WorkerConfig, *, http=None, sleep=time.sleep, clock=time.monotonic):
        self._config = config
        self._clock = clock
        self._http = http or _urllib_http
        self._sleep = sleep
        self._stop = threading.Event()
        self._failures = 0
        self._token = ""  # claim token of the job in hand
        # Seconds ``run`` waits before the next claim (0: claim again at once).
        self.backoff_seconds = config.poll_seconds

    # -------------------------------------------------------------- requests
    def _post_cp(self, path: str, body: dict):
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": read_key(self._config.api_key_file),
        }
        status, hdrs, raw = self._http(
            "POST",
            self._config.control_plane_url + path,
            headers,
            json.dumps(body).encode(),
            CONTROL_PLANE_TIMEOUT,
        )
        if status in (401, 403):
            raise WorkerAuthError("the control plane refused the reviewer key")
        return status, hdrs, raw

    def _call_model(self, messages) -> str:
        m = self._config.model
        headers = {"Content-Type": "application/json"}
        if m.api_key_file is not None:
            headers["Authorization"] = "Bearer " + read_key(m.api_key_file)
        body = {
            "model": m.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": m.max_tokens,
        }
        status, _hdrs, raw = self._http(
            "POST",
            m.base_url + "/v1/chat/completions",
            headers,
            json.dumps(body).encode(),
            m.timeout_seconds,
        )
        if status != 200:
            raise _ModelError()
        obj = _json_object(raw)
        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _BadResponse()
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise _BadResponse()
        if choices[0].get("finish_reason") == "length":
            raise _Truncated()
        return message["content"]

    @staticmethod
    def _heartbeat_interval(lease_seconds) -> float:
        return max(1.0, lease_seconds / 3)

    # ------------------------------------------------------------------ claim
    def run_once(self) -> bool:
        """One claim cycle. True when a job was claimed and handled (whatever the outcome).

        Never sleeps; it sets ``backoff_seconds``, the wait ``run`` honours before
        the next claim (see the module docstring on failure spacing).
        Raises ``WorkerAuthError`` on 401/403 and ``WorkerConfigError`` when a key
        file is no longer acceptable; a held job is then abandoned (its lease expires).
        """
        poll = self._config.poll_seconds
        self.backoff_seconds = poll
        body = {"worker_id": self._config.worker_id, "reviewer_model": self._config.model.model}
        try:
            status, hdrs, raw = self._post_cp(_CLAIM_PATH, body)
        except (WorkerAuthError, WorkerConfigError):
            raise
        except Exception as exc:  # noqa: BLE001 - transport: back off, class name only
            _log.warning("review gate claim failed: %s", type(exc).__name__)
            return False
        if status == 429:
            self.backoff_seconds = _retry_after(hdrs, poll)
            _log.info("review gate claim rate_limited")
            return False
        if status != 200:
            _log.warning("review gate claim claim_error")
            return False
        try:
            reply = _json_object(raw)
        except (_BadResponse, _TooLarge):
            reply = None
        if reply is None or set(reply) != {"job"}:
            _log.warning("review gate claim bad_response")
            return False
        job = reply["job"]
        if job is None:
            self._failures = 0
            return False
        job_id = job.get("job_id") if isinstance(job, dict) else None
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            # Nowhere safe to post a failure: let the lease expire.
            _log.warning("review gate claim bad_response")
            return False
        token = job.get("claim_token")
        if not _token_ok(token):
            # Cannot authenticate a failure post: let the lease expire.
            _log.warning("review gate claim bad_response")
            return False
        self._token = token
        _log.info("review gate job %s claimed", job_id)
        if not _valid_job(job):
            self._post_failure(job_id, "bad_response")
            ok = False
        else:
            ok = self._handle(job_id, job)
        if ok:
            self._failures = 0
            self.backoff_seconds = 0
        else:
            self._failures = min(self._failures + 1, 32)
            self.backoff_seconds = self._failure_backoff()
        return True

    def _failure_backoff(self) -> int:
        delay = self._config.poll_seconds
        for _ in range(self._failures - 1):
            delay = min(delay * 2, MAX_BACKOFF_SECONDS)
        return min(delay, MAX_BACKOFF_SECONDS)

    # -------------------------------------------------------------------- job
    def _handle(self, job_id: str, job: dict) -> bool:
        """Run one job; True only when it ended in a recorded (or stale) result."""
        done, wake = threading.Event(), threading.Event()
        lost = {"lost": False, "auth": False, "config": False}
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, self._heartbeat_interval(job["lease_seconds"]), done, wake, lost),
            name=f"review-gate-heartbeat-{job_id[:8]}",
            daemon=True,
        )
        box: dict = {}

        def call_model():
            try:
                box["text"] = self._call_model(job["messages"])
            except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
                box["error"] = exc
            finally:
                wake.set()

        model = threading.Thread(target=call_model, name="review-gate-model", daemon=True)
        deadline = self._clock() + self._config.model.timeout_seconds
        timed_out = False
        heartbeat.start()
        model.start()
        while not wake.wait(0.1):
            if self._stop.is_set():
                break
            if self._clock() >= deadline:
                timed_out = True  # total wall clock, whatever the transport does
                break
        done.set()
        heartbeat.join(CONTROL_PLANE_TIMEOUT + 5)

        if lost["auth"]:
            _log.warning("review gate job %s abandoned", job_id)
            raise WorkerAuthError("the control plane refused the reviewer key")
        if lost["config"]:
            _log.warning("review gate job %s abandoned", job_id)
            raise WorkerConfigError("key file is no longer acceptable")
        if lost["lost"]:
            _log.info("review gate job %s abandoned", job_id)
            return False
        if timed_out:
            self._post_failure(job_id, "model_timeout")
            return False
        if "text" not in box and "error" not in box:  # stopped mid-call
            _log.info("review gate job %s abandoned", job_id)
            return False
        error = box.get("error")
        if isinstance(error, WorkerConfigError):
            _log.warning("review gate job %s abandoned", job_id)
            raise WorkerConfigError("key file is no longer acceptable")
        if error is not None:
            reason = {
                _ModelError: "model_error",
                _BadResponse: "bad_response",
                _TooLarge: "response_too_large",
                _Truncated: "truncated",
                _DeadlineExceeded: "model_timeout",
            }.get(type(error), type(error).__name__)
            self._post_failure(job_id, reason)
            return False
        return self._post_result(job_id, box["text"])

    def _heartbeat_loop(self, job_id, interval, done, wake, lost):
        path = f"{_JOBS_PATH}{job_id}/heartbeat"
        while not done.wait(interval):
            try:
                status, _hdrs, raw = self._post_cp(
                    path, {"worker_id": self._config.worker_id, "claim_token": self._token}
                )
            except WorkerAuthError:
                lost.update(lost=True, auth=True)
                break
            except WorkerConfigError:
                lost.update(lost=True, config=True)
                break
            except Exception:  # noqa: BLE001 - transient; the next beat decides
                continue
            if status == 429 or status >= 500:
                continue
            if status == 200:
                try:
                    if _json_object(raw) == {"ok": True}:
                        continue
                except (_BadResponse, _TooLarge):
                    pass
            lost["lost"] = True
            break
        if lost["lost"]:
            wake.set()

    def _post_result(self, job_id: str, text: str) -> bool:
        body = {"worker_id": self._config.worker_id, "output": text, "claim_token": self._token}
        try:
            status, _hdrs, raw = self._post_cp(f"{_JOBS_PATH}{job_id}/result", body)
        except (WorkerAuthError, WorkerConfigError):
            raise
        except Exception as exc:  # noqa: BLE001 - class name only
            self._post_failure(job_id, type(exc).__name__)
            return False
        if status == 413:
            self._post_failure(job_id, "output_too_large")
            return False
        if status != 200:
            self._post_failure(job_id, "result_error")
            return False
        # "rejected" spends an attempt server-side, so it backs off like a failure.
        return self._log_outcome(job_id, raw, _RESULT_OUTCOMES) in ("recorded", "stale")

    def _post_failure(self, job_id: str, reason: str) -> None:
        body = {"worker_id": self._config.worker_id, "reason": reason, "claim_token": self._token}
        try:
            status, _hdrs, raw = self._post_cp(f"{_JOBS_PATH}{job_id}/failure", body)
        except (WorkerAuthError, WorkerConfigError):
            raise
        except Exception:  # noqa: BLE001 - the lease expires instead
            _log.warning("review gate job %s failure_not_posted", job_id)
            return
        if status != 200:
            _log.warning("review gate job %s failure_not_posted", job_id)
            return
        self._log_outcome(job_id, raw, _FAILURE_OUTCOMES)

    @staticmethod
    def _log_outcome(job_id, raw, allowed):
        try:
            reply = _json_object(raw)
        except (_BadResponse, _TooLarge):
            reply = None
        outcome = reply.get("outcome") if reply is not None and set(reply) == {"outcome"} else None
        if isinstance(outcome, str) and outcome in allowed:
            _log.info("review gate job %s %s", job_id, outcome)
            return outcome
        _log.warning("review gate job %s bad_response", job_id)
        return None

    # ------------------------------------------------------------------- loop
    def run(self, stop: threading.Event) -> None:
        """Claim and handle jobs until ``stop`` is set.

        Raises ``WorkerAuthError`` (401/403) and ``WorkerConfigError`` (a key file
        went bad) without retrying. A job still in flight when ``stop`` is set is
        abandoned without posting: the server re-offers it when its lease expires.
        That abandoned claim has spent one of the job's ``max_attempts``, so
        restarting workers mid-review again and again can escalate the PR.
        """
        self._stop = stop
        while not stop.is_set():
            try:
                self.run_once()
            except (WorkerAuthError, WorkerConfigError):
                raise
            except Exception as exc:  # noqa: BLE001 - keep serving; class name only
                _log.error("review gate worker error: %s", type(exc).__name__)
                self.backoff_seconds = self._config.poll_seconds
            if self.backoff_seconds > 0 and not stop.is_set():
                stop.wait(self.backoff_seconds)
