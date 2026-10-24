"""Reviewer worker (review gate, plan 2 task 8): config, run_once, run, CLI."""

import http.server
import json
import logging
import threading
import time

import pytest
import yaml

from herdr_engineering import cli
from herdr_engineering.review_gate import worker as worker_mod
from herdr_engineering.review_gate.worker import (
    MAX_RESPONSE_BYTES,
    ModelConfig,
    ReviewWorker,
    WorkerAuthError,
    WorkerConfig,
    load_worker_config,
)

CP = "https://cp.example.org"
MODEL_URL = "https://llm.example.org"
CP_KEY = "rk-reviewer-SECRET-41a7"
MODEL_KEY = "sk-model-SECRET-93be"
JOB_ID = "0123456789abcdef0123456789abcdef"
MESSAGES = [
    {"role": "system", "content": "You review code."},
    {"role": "user", "content": "PROMPT-TEXT-ATTACKER-CONTROLLED"},
]
OUTPUT = json.dumps({"verdict": "PASS", "findings": []})


def write_key(tmp_path, name, text, mode=0o600):
    path = tmp_path / name
    path.write_text(text + "\n")
    path.chmod(mode)
    return str(path)


def make_config(tmp_path, *, model_key=True, poll=7, cp=CP, base=MODEL_URL):
    return WorkerConfig(
        control_plane_url=cp,
        api_key_file=write_key(tmp_path, "cp.key", CP_KEY),
        worker_id="worker-1",
        model=ModelConfig(
            base_url=base,
            model="qwen3-review",
            api_key_file=write_key(tmp_path, "model.key", MODEL_KEY) if model_key else None,
            max_tokens=2048,
            timeout_seconds=60,
        ),
        poll_seconds=poll,
    )


def job(**over):
    j = {
        "job_id": JOB_ID,
        "repo": "github.com/acme/widget",
        "number": 7,
        "sha": "a" * 40,
        "messages": MESSAGES,
        "lease_seconds": 120,
        "attempt": 1,
    }
    j.update(over)
    return j


def chat(content=OUTPUT):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class FakeHttp:
    """Routes by URL suffix; each route is a callable(body_dict) -> (status, headers, bytes)."""

    def __init__(self):
        self.calls = []
        self.routes = {}

    def on(self, suffix, status=200, payload=None, headers=None, raw=None, fn=None):
        def reply(body):
            if fn is not None:
                return fn(body)
            data = raw if raw is not None else json.dumps(payload).encode()
            return status, headers or {}, data

        self.routes[suffix] = reply

    def __call__(self, method, url, headers, body, timeout):
        assert method == "POST"
        assert timeout and timeout > 0
        parsed = json.loads(body)
        self.calls.append((url, dict(headers), parsed))
        for suffix, reply in self.routes.items():
            if url.endswith(suffix):
                result = reply(parsed)
                if isinstance(result, BaseException):
                    raise result
                return result
        raise AssertionError(f"unexpected url {url}")

    def urls(self):
        return [u for u, _, _ in self.calls]

    def bodies(self, suffix):
        return [b for u, _, b in self.calls if u.endswith(suffix)]


def happy(fake, claim_job=None, model=None):
    fake.on("/api/v1/review-gate/claim", payload={"job": claim_job or job()})
    fake.on("/heartbeat", payload={"ok": True})
    fake.on("/v1/chat/completions", payload=model or chat())
    fake.on("/result", payload={"outcome": "recorded"})
    fake.on("/failure", payload={"outcome": "released"})


# ---------------------------------------------------------------------- config
def write_yaml(tmp_path, data):
    path = tmp_path / "worker.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


def good_yaml(tmp_path):
    return {
        "control_plane_url": CP,
        "api_key_file": write_key(tmp_path, "cp.key", CP_KEY),
        "worker_id": "worker-1",
        "poll_seconds": 5,
        "model": {
            "base_url": MODEL_URL,
            "model": "qwen3-review",
            "api_key_file": write_key(tmp_path, "model.key", MODEL_KEY),
            "max_tokens": 1024,
            "timeout_seconds": 90,
        },
    }


def test_config_requires_0600_absolute_key_files_and_rejects_unknown_keys(tmp_path):
    data = good_yaml(tmp_path)
    cfg = load_worker_config(write_yaml(tmp_path, data))
    assert cfg.control_plane_url == CP and cfg.worker_id == "worker-1"
    assert cfg.poll_seconds == 5
    assert cfg.model.model == "qwen3-review" and cfg.model.max_tokens == 1024
    assert cfg.model.timeout_seconds == 90

    def bad(mutate, tmp=tmp_path):
        d = good_yaml(tmp)
        mutate(d)
        with pytest.raises(ValueError) as err:
            load_worker_config(write_yaml(tmp, d))
        assert CP_KEY not in str(err.value) and MODEL_KEY not in str(err.value)
        return str(err.value)

    msg = bad(lambda d: d.update({"sk-PASTED-SECRET-as-a-key": 1}))
    assert "PASTED" not in msg and "unknown key" in msg
    msg = bad(lambda d: d["model"].update({"tools-SECRET": []}))
    assert "SECRET" not in msg and "unknown key" in msg
    bad(lambda d: d.update(api_key_file="relative/cp.key"))
    bad(lambda d: d.pop("worker_id"))
    bad(lambda d: d["model"].pop("model"))
    loose = write_key(tmp_path, "loose.key", CP_KEY, mode=0o644)
    bad(lambda d: d.update(api_key_file=loose))
    bad(lambda d: d["model"].update(api_key_file=loose))
    bad(lambda d: d.update(api_key_file=str(tmp_path)))  # a directory
    bad(lambda d: d.update(api_key_file=str(tmp_path / "missing.key")))
    empty = write_key(tmp_path, "empty.key", "  ")
    bad(lambda d: d.update(api_key_file=empty))
    bad(lambda d: d.update(worker_id="has space"))
    bad(lambda d: d["model"].update(model="bad model"))
    bad(lambda d: d.update(poll_seconds=0))
    bad(lambda d: d.update(poll_seconds=True))
    bad(lambda d: d["model"].update(max_tokens="many"))

    # model api_key_file is optional; defaults apply
    d = good_yaml(tmp_path)
    del d["model"]["api_key_file"], d["model"]["max_tokens"], d["model"]["timeout_seconds"]
    del d["poll_seconds"]
    cfg = load_worker_config(write_yaml(tmp_path, d))
    assert cfg.model.api_key_file is None and cfg.model.max_tokens == 4096
    assert cfg.model.timeout_seconds == 300 and cfg.poll_seconds == 10

    # a symlink to a 0600 regular file is followed and accepted
    link = tmp_path / "link.key"
    link.symlink_to(tmp_path / "cp.key")
    d = good_yaml(tmp_path)
    d["api_key_file"] = str(link)
    assert load_worker_config(write_yaml(tmp_path, d)).api_key_file == str(link)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://cp.example.org",
        "file:///etc/passwd",
        "https://user:pw@cp.example.org",
        "https://token@cp.example.org",
        "https://cp.example.org/?k=v",
        "https://cp.example.org/#frag",
        "http://cp.example.org",  # plain http to a non-loopback host
        "http://10.0.0.5:8080",
        "https://",
        "https://cp.example.org:notaport",
        "cp.example.org",
    ],
)
def test_config_rejects_bad_urls_without_echoing_them(tmp_path, url):
    for where in ("cp", "model"):
        d = good_yaml(tmp_path)
        if where == "cp":
            d["control_plane_url"] = url
        else:
            d["model"]["base_url"] = url
        with pytest.raises(ValueError) as err:
            load_worker_config(write_yaml(tmp_path, d))
        assert "pw@" not in str(err.value) and "token@" not in str(err.value)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8080", "http://localhost:9", "http://[::1]:7", "https://x.org/p/"]
)
def test_config_accepts_loopback_http_and_any_https(tmp_path, url):
    d = good_yaml(tmp_path)
    d["control_plane_url"] = url
    d["model"]["base_url"] = url
    cfg = load_worker_config(write_yaml(tmp_path, d))
    assert not cfg.control_plane_url.endswith("/")


def test_config_rejects_non_mapping_and_bad_yaml(tmp_path):
    p = tmp_path / "w.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ValueError):
        load_worker_config(str(p))
    p.write_text("a: [unclosed\n")
    with pytest.raises(ValueError):
        load_worker_config(str(p))
    with pytest.raises(ValueError):
        load_worker_config(str(tmp_path / "missing.yaml"))


# -------------------------------------------------------------------- run_once
def test_run_once_idle_returns_false(tmp_path):
    fake = FakeHttp()
    fake.on("/api/v1/review-gate/claim", payload={"job": None})
    w = ReviewWorker(make_config(tmp_path), http=fake)
    assert w.run_once() is False
    assert fake.urls() == [f"{CP}/api/v1/review-gate/claim"]
    assert fake.bodies("/claim") == [{"worker_id": "worker-1", "reviewer_model": "qwen3-review"}]


def test_run_once_posts_model_output_as_result(tmp_path):
    fake = FakeHttp()
    happy(fake)
    w = ReviewWorker(make_config(tmp_path), http=fake)
    assert w.run_once() is True
    (model_body,) = fake.bodies("/v1/chat/completions")
    assert model_body == {
        "model": "qwen3-review",
        "messages": MESSAGES,
        "temperature": 0,
        "max_tokens": 2048,
    }
    assert "tools" not in model_body and "functions" not in model_body
    assert fake.bodies("/result") == [{"worker_id": "worker-1", "output": OUTPUT}]
    assert fake.bodies("/failure") == []
    assert f"{MODEL_URL}/v1/chat/completions" in fake.urls()
    assert f"{CP}/api/v1/review-gate/jobs/{JOB_ID}/result" in fake.urls()


def test_every_control_plane_body_has_exact_keys(tmp_path):
    fake = FakeHttp()
    happy(fake)
    fake.on("/heartbeat", fn=lambda b: (200, {}, b'{"ok": true}'))
    cfg = make_config(tmp_path)
    w = ReviewWorker(cfg, http=fake)
    fake.on(
        "/v1/chat/completions",
        fn=lambda b: (time.sleep(0.2), (200, {}, json.dumps(chat()).encode()))[1],
    )
    w._heartbeat_interval = lambda lease: 0.02
    assert w.run_once() is True
    expected = {
        "/claim": {"worker_id", "reviewer_model"},
        "/heartbeat": {"worker_id"},
        "/result": {"worker_id", "output"},
    }
    for suffix, keys in expected.items():
        bodies = fake.bodies(suffix)
        assert bodies, suffix
        assert all(set(b) == keys for b in bodies), suffix
    # a failure body too
    fake2 = FakeHttp()
    happy(fake2)
    fake2.on("/v1/chat/completions", status=500, payload={"error": "boom"})
    assert ReviewWorker(cfg, http=fake2).run_once() is True
    assert [set(b) for b in fake2.bodies("/failure")] == [{"worker_id", "reason"}]


def test_model_error_posts_failure_without_body_text(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeHttp()
    happy(fake)
    fake.on("/v1/chat/completions", status=500, raw=b"MODEL-SECRET-ERROR-BODY at https://x")
    w = ReviewWorker(make_config(tmp_path), http=fake)
    assert w.run_once() is True
    (failure,) = fake.bodies("/failure")
    assert failure["worker_id"] == "worker-1"
    reason = failure["reason"]
    assert reason == "model_error"
    assert fake.bodies("/result") == []
    assert "MODEL-SECRET-ERROR-BODY" not in caplog.text and MODEL_URL not in caplog.text

    # a transport exception: class name only
    class Boom(OSError):
        pass

    fake2 = FakeHttp()
    happy(fake2)
    fake2.on("/v1/chat/completions", fn=lambda b: Boom(f"refused {MODEL_URL} secret"))
    assert ReviewWorker(make_config(tmp_path), http=fake2).run_once() is True
    assert fake2.bodies("/failure") == [{"worker_id": "worker-1", "reason": "Boom"}]
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": ["x"]}}]},
        {"choices": [{"message": "x"}]},
        {"choices": [None]},
        {"nope": 1},
        [1, 2],
    ],
)
def test_unexpected_model_shapes_are_bad_response(tmp_path, payload):
    fake = FakeHttp()
    happy(fake, model=payload)
    assert ReviewWorker(make_config(tmp_path), http=fake).run_once() is True
    assert fake.bodies("/failure") == [{"worker_id": "worker-1", "reason": "bad_response"}]
    assert fake.bodies("/result") == []


def test_non_json_model_body_is_bad_response(tmp_path):
    fake = FakeHttp()
    happy(fake)
    fake.on("/v1/chat/completions", raw=b"<html>not json</html>")
    assert ReviewWorker(make_config(tmp_path), http=fake).run_once() is True
    assert fake.bodies("/failure") == [{"worker_id": "worker-1", "reason": "bad_response"}]


def test_authorization_headers_use_key_files_and_never_appear_in_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeHttp()
    happy(fake)
    w = ReviewWorker(make_config(tmp_path), http=fake)
    assert w.run_once() is True
    for url, headers, _ in fake.calls:
        assert headers["Content-Type"] == "application/json"
        if url.startswith(CP):
            assert headers["X-API-Key"] == CP_KEY
            assert "Authorization" not in headers
        else:
            assert headers["Authorization"] == f"Bearer {MODEL_KEY}"
            assert "X-API-Key" not in headers
    assert CP_KEY not in caplog.text and MODEL_KEY not in caplog.text
    assert "PROMPT-TEXT" not in caplog.text and OUTPUT not in caplog.text
    assert JOB_ID in caplog.text

    # no model key file -> no Authorization header at all
    fake2 = FakeHttp()
    happy(fake2)
    assert ReviewWorker(make_config(tmp_path, model_key=False), http=fake2).run_once()
    model_headers = [h for u, h, _ in fake2.calls if u.startswith(MODEL_URL)]
    assert model_headers and all("Authorization" not in h for h in model_headers)


def test_keys_are_reread_on_every_call(tmp_path):
    fake = FakeHttp()
    fake.on("/api/v1/review-gate/claim", payload={"job": None})
    cfg = make_config(tmp_path)
    w = ReviewWorker(cfg, http=fake)
    assert w.run_once() is False
    with open(cfg.api_key_file, "w") as fh:
        fh.write("rotated-key-2\n")
    assert w.run_once() is False
    assert [h["X-API-Key"] for _, h, _ in fake.calls] == [CP_KEY, "rotated-key-2"]
    # a key file that loses its 0600 mode is refused at the next call, fail closed
    import os

    os.chmod(cfg.api_key_file, 0o644)
    with pytest.raises(ValueError) as err:
        w.run_once()
    assert "rotated-key-2" not in str(err.value)
    assert len(fake.calls) == 2


def test_lost_heartbeat_abandons_without_posting(tmp_path):
    fake = FakeHttp()
    happy(fake)
    fake.on("/heartbeat", payload={"ok": False})
    model_started = threading.Event()
    release = threading.Event()

    def slow_model(body):
        model_started.set()
        release.wait(5)
        return 200, {}, json.dumps(chat()).encode()

    fake.on("/v1/chat/completions", fn=slow_model)
    w = ReviewWorker(make_config(tmp_path), http=fake)
    w._heartbeat_interval = lambda lease: 0.01
    t0 = time.monotonic()
    assert w.run_once() is True
    elapsed = time.monotonic() - t0
    release.set()
    assert elapsed < 2  # stopped waiting on the model once the lease was lost
    assert fake.bodies("/result") == [] and fake.bodies("/failure") == []


@pytest.mark.parametrize("status", [404, 409, 400])
def test_heartbeat_4xx_abandons_without_posting(tmp_path, status):
    fake = FakeHttp()
    happy(fake)
    fake.on("/heartbeat", status=status, payload={"error": "x"})
    fake.on(
        "/v1/chat/completions",
        fn=lambda b: (time.sleep(0.3), (200, {}, json.dumps(chat()).encode()))[1],
    )
    w = ReviewWorker(make_config(tmp_path), http=fake)
    w._heartbeat_interval = lambda lease: 0.01
    assert w.run_once() is True
    assert fake.bodies("/result") == [] and fake.bodies("/failure") == []


def test_heartbeat_429_is_not_a_lost_lease(tmp_path):
    fake = FakeHttp()
    happy(fake)
    fake.on("/heartbeat", status=429, payload={"error": "slow down"}, headers={"Retry-After": "1"})
    fake.on(
        "/v1/chat/completions",
        fn=lambda b: (time.sleep(0.2), (200, {}, json.dumps(chat()).encode()))[1],
    )
    w = ReviewWorker(make_config(tmp_path), http=fake)
    w._heartbeat_interval = lambda lease: 0.01
    assert w.run_once() is True
    assert len(fake.bodies("/heartbeat")) >= 1
    assert fake.bodies("/result") == [{"worker_id": "worker-1", "output": OUTPUT}]


def test_heartbeat_auth_error_abandons_then_raises(tmp_path):
    fake = FakeHttp()
    happy(fake)
    fake.on("/heartbeat", status=401, payload={"error": "unauthorized"})
    fake.on(
        "/v1/chat/completions",
        fn=lambda b: (time.sleep(0.3), (200, {}, json.dumps(chat()).encode()))[1],
    )
    w = ReviewWorker(make_config(tmp_path), http=fake)
    w._heartbeat_interval = lambda lease: 0.01
    with pytest.raises(WorkerAuthError):
        w.run_once()
    assert fake.bodies("/result") == [] and fake.bodies("/failure") == []


def test_heartbeat_thread_stops_when_job_finishes(tmp_path):
    fake = FakeHttp()
    happy(fake)
    w = ReviewWorker(make_config(tmp_path), http=fake)
    w._heartbeat_interval = lambda lease: 0.01
    before = {t.ident for t in threading.enumerate()}
    assert w.run_once() is True
    time.sleep(0.05)
    beats = len(fake.bodies("/heartbeat"))
    time.sleep(0.1)
    assert len(fake.bodies("/heartbeat")) == beats
    leftover = [t for t in threading.enumerate() if t.ident not in before and t.is_alive()]
    assert [t for t in leftover if t.name.startswith("review-gate-heartbeat")] == []


def test_oversized_model_response_is_a_failure(tmp_path):
    fake = FakeHttp()
    happy(fake)
    fake.on("/v1/chat/completions", raw=b" " * (MAX_RESPONSE_BYTES + 1))
    assert ReviewWorker(make_config(tmp_path), http=fake).run_once() is True
    assert fake.bodies("/failure") == [{"worker_id": "worker-1", "reason": "response_too_large"}]
    assert fake.bodies("/result") == []


def test_result_413_posts_output_too_large(tmp_path):
    fake = FakeHttp()
    happy(fake)
    fake.on("/result", status=413, payload={"error": "request body too large"})
    assert ReviewWorker(make_config(tmp_path), http=fake).run_once() is True
    assert fake.bodies("/failure") == [{"worker_id": "worker-1", "reason": "output_too_large"}]


def test_result_transport_error_posts_failure(tmp_path):
    fake = FakeHttp()
    happy(fake)
    fake.on("/result", fn=lambda b: BrokenPipeError("pipe"))
    assert ReviewWorker(make_config(tmp_path), http=fake).run_once() is True
    assert fake.bodies("/failure") == [{"worker_id": "worker-1", "reason": "BrokenPipeError"}]


def test_unexpected_result_outcome_is_logged_not_trusted(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    fake = FakeHttp()
    happy(fake)
    fake.on("/result", payload={"outcome": "approved-by-magic"})
    assert ReviewWorker(make_config(tmp_path), http=fake).run_once() is True
    assert "approved-by-magic" not in caplog.text
    assert "bad_response" in caplog.text


@pytest.mark.parametrize("status", [401, 403])
def test_claim_auth_error_raises(tmp_path, status):
    fake = FakeHttp()
    fake.on("/api/v1/review-gate/claim", status=status, payload={"error": "no"})
    with pytest.raises(WorkerAuthError):
        ReviewWorker(make_config(tmp_path), http=fake).run_once()


@pytest.mark.parametrize("status", [401, 403])
def test_result_auth_error_raises(tmp_path, status):
    fake = FakeHttp()
    happy(fake)
    fake.on("/result", status=status, payload={"error": "no"})
    with pytest.raises(WorkerAuthError):
        ReviewWorker(make_config(tmp_path), http=fake).run_once()
    assert fake.bodies("/failure") == []


def test_claim_429_backs_off_with_retry_after(tmp_path):
    fake = FakeHttp()
    fake.on("/api/v1/review-gate/claim", status=429, payload={}, headers={"Retry-After": "42"})
    w = ReviewWorker(make_config(tmp_path, poll=7), http=fake)
    assert w.run_once() is False
    assert w.backoff_seconds == 42
    fake.on("/api/v1/review-gate/claim", status=429, payload={}, headers={"Retry-After": "9999"})
    w.run_once()
    assert w.backoff_seconds == 300
    for bad in ("soon", "-3", "0", ""):
        fake.on("/api/v1/review-gate/claim", status=429, payload={}, headers={"Retry-After": bad})
        w.run_once()
        assert w.backoff_seconds == 7
    fake.on("/api/v1/review-gate/claim", payload={"job": None})
    w.run_once()
    assert w.backoff_seconds == 7
    assert all(not u.endswith("/failure") for u in fake.urls())


def test_claim_transport_and_bad_json_back_off(tmp_path):
    fake = FakeHttp()
    fake.on("/api/v1/review-gate/claim", fn=lambda b: ConnectionRefusedError("x"))
    w = ReviewWorker(make_config(tmp_path), http=fake)
    assert w.run_once() is False
    for raw in (b"not json", b'{"job": 5}', b'{"job": null, "x": 1}', b"[]"):
        fake.on("/api/v1/review-gate/claim", raw=raw)
        assert w.run_once() is False
    fake.on("/api/v1/review-gate/claim", status=500, payload={"error": "x"})
    assert w.run_once() is False
    assert all(u.endswith("/claim") for u in fake.urls())


def test_bad_job_id_is_never_used_in_a_url(tmp_path):
    fake = FakeHttp()
    happy(fake, claim_job=job(job_id="../../admin"))
    assert ReviewWorker(make_config(tmp_path), http=fake).run_once() is False
    assert all(u.endswith("/claim") for u in fake.urls())


@pytest.mark.parametrize(
    "over",
    [
        {"messages": "text"},
        {"messages": [1]},
        {"lease_seconds": 0},
        {"lease_seconds": True},
        {"extra": 1},
    ],
)
def test_malformed_job_posts_bad_response(tmp_path, over):
    fake = FakeHttp()
    happy(fake, claim_job=job(**over))
    assert ReviewWorker(make_config(tmp_path), http=fake).run_once() is True
    assert fake.bodies("/failure") == [{"worker_id": "worker-1", "reason": "bad_response"}]
    assert fake.bodies("/v1/chat/completions") == []


# ------------------------------------------------------------------------- run
def test_run_stops_promptly_while_idle(tmp_path):
    fake = FakeHttp()
    fake.on("/api/v1/review-gate/claim", payload={"job": None})
    w = ReviewWorker(make_config(tmp_path, poll=600), http=fake)
    stop = threading.Event()
    t = threading.Thread(target=w.run, args=(stop,))
    t.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    stop.set()
    t.join(2)
    assert not t.is_alive() and time.monotonic() - t0 < 1
    assert len(fake.calls) == 1


def test_run_stops_promptly_during_a_model_call(tmp_path):
    fake = FakeHttp()
    happy(fake)
    release = threading.Event()
    fake.on(
        "/v1/chat/completions",
        fn=lambda b: (release.wait(5), (200, {}, json.dumps(chat()).encode()))[1],
    )
    w = ReviewWorker(make_config(tmp_path), http=fake)
    stop = threading.Event()
    t = threading.Thread(target=w.run, args=(stop,))
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(2)
    alive = t.is_alive()
    release.set()
    assert not alive
    # abandoned on stop: the lease expires server-side; nothing posted
    assert fake.bodies("/result") == [] and fake.bodies("/failure") == []


def test_run_raises_auth_error_without_retrying(tmp_path):
    fake = FakeHttp()
    fake.on("/api/v1/review-gate/claim", status=403, payload={"error": "no"})
    w = ReviewWorker(make_config(tmp_path), http=fake)
    with pytest.raises(WorkerAuthError):
        w.run(threading.Event())
    assert len(fake.calls) == 1


def test_run_backs_off_on_429_then_continues(tmp_path):
    fake = FakeHttp()
    replies = iter([(429, {"Retry-After": "1"}, b"{}"), (200, {}, b'{"job": null}')])
    stop = threading.Event()

    def claim(body):
        try:
            return next(replies)
        except StopIteration:
            stop.set()
            return 200, {}, b'{"job": null}'

    fake.on("/api/v1/review-gate/claim", fn=claim)
    w = ReviewWorker(make_config(tmp_path, poll=1), http=fake)
    t0 = time.monotonic()
    w.run(stop)
    assert 1.8 <= time.monotonic() - t0 < 5
    assert len(fake.calls) == 3


# ---------------------------------------------------------- default transport
class _Handler(http.server.BaseHTTPRequestHandler):
    mode = "big"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.2:1/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"x" * (MAX_RESPONSE_BYTES * 2)
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


@pytest.fixture
def local_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_default_transport_refuses_redirects(local_server):
    _Handler.mode = "redirect"
    url = f"http://127.0.0.1:{local_server.server_address[1]}/x"
    status, _headers, body = worker_mod._urllib_http("POST", url, {}, b"{}", 5)
    assert status == 302 and body == b""


def test_default_transport_caps_the_read(local_server):
    _Handler.mode = "big"
    url = f"http://127.0.0.1:{local_server.server_address[1]}/x"
    status, _headers, body = worker_mod._urllib_http("POST", url, {}, b"{}", 5)
    assert status == 200 and len(body) == MAX_RESPONSE_BYTES + 1


def test_default_transport_ignores_proxy_environment(monkeypatch, local_server):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    _Handler.mode = "big"
    url = f"http://127.0.0.1:{local_server.server_address[1]}/x"
    status, _h, _b = worker_mod._urllib_http("POST", url, {}, b"{}", 5)
    assert status == 200


# ------------------------------------------------------------------------- CLI
def test_cli_worker_once_runs_a_single_cycle(tmp_path, monkeypatch, capsys):
    path = write_yaml(tmp_path, good_yaml(tmp_path))
    seen = []

    class FakeWorker:
        def __init__(self, config):
            seen.append(config)

        def run_once(self):
            seen.append("once")
            return False

        def run(self, stop):
            raise AssertionError("--once must not loop")

    monkeypatch.setattr(worker_mod, "ReviewWorker", FakeWorker)
    assert cli.main(["review-gate", "worker", "--config", path, "--once"]) == 0
    assert capsys.readouterr().out.strip() == "idle"
    assert isinstance(seen[0], WorkerConfig) and seen[1:] == ["once"]

    FakeWorker.run_once = lambda self: True
    assert cli.main(["review-gate", "worker", "--config", path, "--once"]) == 0
    assert capsys.readouterr().out.strip() == "handled"


def test_cli_worker_config_error_exits_2_without_key_material(tmp_path, capsys):
    d = good_yaml(tmp_path)
    d["api_key_file"] = write_key(tmp_path, "loose.key", CP_KEY, mode=0o644)
    path = write_yaml(tmp_path, d)
    assert cli.main(["review-gate", "worker", "--config", path, "--once"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and len(out.err.strip().splitlines()) == 1
    assert CP_KEY not in out.err


def test_cli_worker_auth_error_exits_1(tmp_path, monkeypatch, capsys):
    path = write_yaml(tmp_path, good_yaml(tmp_path))

    class FakeWorker:
        def __init__(self, config):
            pass

        def run_once(self):
            raise WorkerAuthError("control plane refused the reviewer key")

        def run(self, stop):
            raise WorkerAuthError("control plane refused the reviewer key")

    monkeypatch.setattr(worker_mod, "ReviewWorker", FakeWorker)
    assert cli.main(["review-gate", "worker", "--config", path, "--once"]) == 1
    assert cli.main(["review-gate", "worker", "--config", path]) == 1
    assert CP_KEY not in capsys.readouterr().err


def test_cli_worker_loop_installs_signal_handlers(tmp_path, monkeypatch):
    path = write_yaml(tmp_path, good_yaml(tmp_path))
    installed = {}
    monkeypatch.setattr(cli.signal, "signal", lambda sig, fn: installed.setdefault(sig, fn))

    class FakeWorker:
        def __init__(self, config):
            pass

        def run(self, stop):
            installed[cli.signal.SIGTERM](cli.signal.SIGTERM, None)
            assert stop.is_set()

    monkeypatch.setattr(worker_mod, "ReviewWorker", FakeWorker)
    assert cli.main(["review-gate", "worker", "--config", path]) == 0
    assert set(installed) == {cli.signal.SIGINT, cli.signal.SIGTERM}


# ------------------------------------------------------------- end to end
class _FakeModel(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeModel.seen.append((self.path, self.headers.get("Authorization"), body))
        data = json.dumps(chat()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_end_to_end_against_the_real_control_plane(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="herdr_engineering.review_gate.worker")
    """Wire format check: the real ControlPlaneServer (Task 7 routes) + a loopback fake model."""
    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.ci_monitor import CIMonitor
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    from herdr_engineering.review_gate import github_app
    from herdr_engineering.review_gate.models import PrKey, State
    from tests.control_plane.test_review_gate_reviewer_api import (
        FakeApp,
        FakeFleet,
        gate_section,
    )

    monkeypatch.setattr(CIMonitor, "start", lambda self: None)
    monkeypatch.setattr(github_app, "GitHubAppClient", FakeApp)
    srv = ControlPlaneServer(
        ControlPlaneRepo(),
        api_keys={CP_KEY: "reviewer", "op": "operator"},
        fleet_config=FakeFleet(gate_section(tmp_path)),
    )
    srv.gate_service.stop()
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    model = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    _FakeModel.seen = []
    try:
        key = PrKey("github.com/acme/widget", 7)
        sha = "a" * 40
        srv.gate_store.on_push(key, sha)
        srv.gate_store.on_ci_green(key, sha)
        queued = srv.gate_queue.enqueue(key, sha, "author-model")
        cfg = make_config(
            tmp_path,
            cp=f"http://127.0.0.1:{srv.port}",
            base=f"http://127.0.0.1:{model.server_address[1]}",
        )
        w = ReviewWorker(cfg)
        assert w.run_once() is True
        (path, auth, body) = _FakeModel.seen[0]
        assert path == "/v1/chat/completions" and auth == f"Bearer {MODEL_KEY}"
        assert body["temperature"] == 0 and "tools" not in body
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert srv.gate_store.snapshot(key).state != State.REVIEWING
        assert f"review gate job {queued.job_id} recorded" in caplog.text
        # the job is finished server-side: posting again is stale
        status, _h, raw = w._post_cp(
            f"/api/v1/review-gate/jobs/{queued.job_id}/result",
            {"worker_id": "worker-1", "output": OUTPUT},
        )
        assert (status, json.loads(raw)) == (200, {"outcome": "stale"})
        assert w.run_once() is False  # nothing left to claim
    finally:
        model.shutdown()
        model.server_close()
        srv.shutdown()


# ------------------------------------------------------------- fix round 1
class RecordingStop(threading.Event):
    """A stop event whose wait() returns at once and records the requested delay."""

    def __init__(self, log, limit):
        super().__init__()
        self.log, self.limit = log, limit

    def wait(self, timeout=None):
        self.log.append(("wait", timeout))
        if sum(1 for e in self.log if e[0] == "wait") >= self.limit:
            self.set()
        return self.is_set()


def test_failed_jobs_back_off_exponentially_and_reset_after_success(tmp_path):
    log = []
    fake = FakeHttp()
    happy(fake)
    model_ok = {"ok": False}

    def model(body):
        log.append(("model",))
        if sum(1 for e in log if e == ("model",)) > 20:
            stop.set()  # guard: never loop forever if run() ignores the backoff
        if model_ok["ok"]:
            return 200, {}, json.dumps(chat()).encode()
        return 500, {}, b"down"

    fake.on("/v1/chat/completions", fn=model)
    w = ReviewWorker(make_config(tmp_path, poll=10), http=fake)
    stop = RecordingStop(log, limit=7)
    w.run(stop)
    # every model failure is followed by a wait before the next claim
    assert log[0] == ("model",)
    for i, entry in enumerate(log):
        if entry == ("model",) and i + 1 < len(log):
            assert log[i + 1][0] == "wait" and log[i + 1][1] >= 10
    waits = [t for kind, *rest in log if kind == "wait" for t in rest]
    assert waits == [10, 20, 40, 80, 160, 300, 300]
    assert len(fake.bodies("/failure")) == 7

    # a recorded result resets: no wait, then an idle claim waits poll_seconds
    model_ok["ok"] = True
    assert w.run_once() is True and w.backoff_seconds == 0
    model_ok["ok"] = False
    assert w.run_once() is True and w.backoff_seconds == 10
    fake.on("/api/v1/review-gate/claim", payload={"job": None})
    assert w.run_once() is False and w.backoff_seconds == 10
    fake.on("/api/v1/review-gate/claim", payload={"job": job()})
    assert w.run_once() is True and w.backoff_seconds == 10  # reset by the idle claim


@pytest.mark.parametrize("ending", ["result_429", "rejected", "failure_not_posted", "lost"])
def test_other_unsuccessful_endings_also_back_off(tmp_path, ending):
    fake = FakeHttp()
    happy(fake)
    if ending == "result_429":
        fake.on("/result", status=429, payload={})
    elif ending == "rejected":
        fake.on("/result", payload={"outcome": "rejected"})
    elif ending == "failure_not_posted":
        fake.on("/v1/chat/completions", status=500, payload={})
        fake.on("/failure", fn=lambda b: ConnectionRefusedError("x"))
    else:
        fake.on("/heartbeat", payload={"ok": False})
        fake.on(
            "/v1/chat/completions",
            fn=lambda b: (time.sleep(0.3), (200, {}, json.dumps(chat()).encode()))[1],
        )
    w = ReviewWorker(make_config(tmp_path, poll=10), http=fake)
    w._heartbeat_interval = lambda lease: 0.01
    assert w.run_once() is True
    assert w.backoff_seconds == 10


def test_lease_seconds_above_a_day_is_bad_response(tmp_path):
    fake = FakeHttp()
    happy(fake, claim_job=job(lease_seconds=86401))
    assert ReviewWorker(make_config(tmp_path), http=fake).run_once() is True
    assert fake.bodies("/failure") == [{"worker_id": "worker-1", "reason": "bad_response"}]
    assert fake.bodies("/v1/chat/completions") == []
    fake2 = FakeHttp()
    happy(fake2, claim_job=job(lease_seconds=86400))
    assert ReviewWorker(make_config(tmp_path), http=fake2).run_once() is True
    assert fake2.bodies("/result")


@pytest.mark.parametrize("value", ["\u00b2", "9" * 5000, "1234567", " 12x"])
def test_odd_retry_after_values_fall_back_to_poll(tmp_path, value):
    fake = FakeHttp()
    fake.on("/api/v1/review-gate/claim", status=429, payload={}, headers={"Retry-After": value})
    w = ReviewWorker(make_config(tmp_path, poll=7), http=fake)
    assert w.run_once() is False
    assert w.backoff_seconds == 7


def test_odd_retry_after_does_not_break_cli_once(tmp_path, monkeypatch, capsys):
    path = write_yaml(tmp_path, good_yaml(tmp_path))
    fake = FakeHttp()
    fake.on(
        "/api/v1/review-gate/claim", status=429, payload={}, headers={"Retry-After": "9" * 5000}
    )
    real = worker_mod.ReviewWorker
    monkeypatch.setattr(worker_mod, "ReviewWorker", lambda cfg: real(cfg, http=fake))
    assert cli.main(["review-gate", "worker", "--config", path, "--once"]) == 0
    assert capsys.readouterr().out.strip() == "idle"


def test_truncated_model_output_is_a_failure(tmp_path):
    fake = FakeHttp()
    payload = {"choices": [{"message": {"content": OUTPUT[:5]}, "finish_reason": "length"}]}
    happy(fake, model=payload)
    assert ReviewWorker(make_config(tmp_path), http=fake).run_once() is True
    assert fake.bodies("/failure") == [{"worker_id": "worker-1", "reason": "truncated"}]
    assert fake.bodies("/result") == []
    fake2 = FakeHttp()
    happy(fake2, model={"choices": [{"message": {"content": OUTPUT}, "finish_reason": "stop"}]})
    assert ReviewWorker(make_config(tmp_path), http=fake2).run_once() is True
    assert fake2.bodies("/result") == [{"worker_id": "worker-1", "output": OUTPUT}]


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "system"}],
        [{"role": "tool", "content": "x"}],
        [{"role": "user", "content": ["x"]}],
        [{"role": "user", "content": "x", "tool_calls": []}],
        [{"role": "user", "content": "x"}, "x"],
    ],
)
def test_bad_message_shapes_are_bad_response_without_a_model_call(tmp_path, messages):
    fake = FakeHttp()
    happy(fake, claim_job=job(messages=messages))
    assert ReviewWorker(make_config(tmp_path), http=fake).run_once() is True
    assert fake.bodies("/failure") == [{"worker_id": "worker-1", "reason": "bad_response"}]
    assert fake.bodies("/v1/chat/completions") == []
