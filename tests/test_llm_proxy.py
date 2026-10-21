import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.llm_proxy import ProxyState, make_handler


class RepoControl:
    """Control-plane client double backed by a real repo."""

    def __init__(self, repo):
        self.repo = repo

    def config(self):
        return self.repo.llm_config()

    def report(self, rec):
        self.repo.record_token_usage(
            rec["project"], rec["model"], rec.get("input_tokens"), rec.get("output_tokens"),
            reasoning_tokens=rec.get("reasoning_tokens"), success=rec.get("success", True),
            key_id=rec.get("key_id"))


def _upstream(seen):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"auth": self.headers.get("Authorization"), "body": body,
                         "headers": {k.lower(): v for k, v in self.headers.items()}})
            usage = {"prompt_tokens": 7, "completion_tokens": 3,
                     "completion_tokens_details": {"reasoning_tokens": 1}}
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n')
                self.wfile.write(b"data: " + json.dumps({"choices": [], "usage": usage}).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                return
            out = json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": usage}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _setup():
    seen, seen_custom = [], []
    up, custom = _upstream(seen), _upstream(seen_custom)
    repo = ControlPlaneRepo()
    minted = repo.create_project_key("proj-a")
    repo.upsert_custom_model("my-model", f"http://127.0.0.1:{custom.server_address[1]}", "real-name")
    state = ProxyState(RepoControl(repo), f"http://127.0.0.1:{up.server_address[1]}", "UPSTREAM-SECRET")
    state.refresh(force=True)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    return repo, minted["key"], proxy, seen, seen_custom, (up, custom)


def _post(proxy, key, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_address[1]}/v1/chat/completions",
        data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {key}",
                                                  "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read()


def test_tokens_attributed_to_project_and_upstream_key_swapped():
    repo, key, proxy, seen, _, srvs = _setup()
    try:
        status, _ = _post(proxy, key, {"model": "qwen", "messages": []})
        assert status == 200
        assert seen[0]["auth"] == "Bearer UPSTREAM-SECRET"  # project key never reaches upstream
        t = repo.token_analytics(project="proj-a")["total"]
        assert (t["requests"], t["input_tokens"], t["output_tokens"], t["reasoning_tokens"]) == (1, 7, 3, 1)
    finally:
        proxy.shutdown()
        [s.shutdown() for s in srvs]


def test_streaming_usage_is_captured_and_requested():
    repo, key, proxy, seen, _, srvs = _setup()
    try:
        _, body = _post(proxy, key, {"model": "qwen", "stream": True, "messages": []})
        assert b"[DONE]" in body
        assert seen[0]["body"]["stream_options"]["include_usage"] is True
        assert repo.token_analytics()["by_model"]["qwen"]["output_tokens"] == 3
    finally:
        proxy.shutdown()
        [s.shutdown() for s in srvs]


def test_missing_or_revoked_key_is_refused_and_not_forwarded():
    repo, key, proxy, seen, _, srvs = _setup()
    try:
        for bad in ("nope", ""):
            try:
                _post(proxy, bad, {"model": "qwen"})
                raise AssertionError("expected 401")
            except urllib.error.HTTPError as e:
                assert e.code == 401
        assert seen == []
    finally:
        proxy.shutdown()
        [s.shutdown() for s in srvs]


def test_custom_model_routes_to_its_endpoint_with_upstream_name_and_no_lab_key():
    repo, key, proxy, seen, seen_custom, srvs = _setup()
    try:
        _post(proxy, key, {"model": "my-model", "messages": []})
        assert seen == [] and seen_custom[0]["body"]["model"] == "real-name"
        assert seen_custom[0]["auth"] is None  # the lab key never goes to a custom endpoint
        assert repo.token_analytics()["by_model"]["my-model"]["requests"] == 1
    finally:
        proxy.shutdown()
        [s.shutdown() for s in srvs]


def test_keys_are_stored_hashed_and_revocable():
    repo = ControlPlaneRepo()
    minted = repo.create_project_key("p")
    assert minted["key"] not in json.dumps(repo.llm_config())
    assert "key_hash" not in json.dumps(repo.list_project_keys())
    repo.revoke_project_key(minted["key_id"])
    assert repo.llm_config()["keys"] == []


def test_key_minted_after_last_refresh_works_without_waiting():
    import time as _t
    repo, key, proxy, seen, _, srvs = _setup()
    try:
        fresh = repo.create_project_key("proj-b")["key"]
        _t.sleep(2.1)  # past the miss-reload rate limit
        assert _post(proxy, fresh, {"model": "qwen", "messages": []})[0] == 200
    finally:
        proxy.shutdown()
        [s.shutdown() for s in srvs]


def test_hydrated_string_payloads_are_understood():
    repo = ControlPlaneRepo()
    minted = repo.create_project_key("p")
    repo.record_token_usage("p", "m", 1, 2)
    state = repo.export_state()
    for stream in state["events"].values():  # what Postgres hydration yields
        for ev in stream:
            ev["payload"] = json.dumps(ev["payload"])
    fresh = ControlPlaneRepo()
    fresh.import_state(state)
    assert fresh.llm_config()["keys"][0]["key_id"] == minted["key_id"]
    assert fresh.token_analytics()["total"]["output_tokens"] == 2


def _post_headers(proxy, key, body, extra=None):
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", **(extra or {})}
    req = urllib.request.Request(f"http://127.0.0.1:{proxy.server_address[1]}/v1/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read()


def _teardown(proxy, srvs):
    proxy.shutdown()
    [s.shutdown() for s in srvs]


def test_gateway_identity_defaults_to_project_and_key():
    repo, key, proxy, seen, _, srvs = _setup()
    try:
        _post_headers(proxy, key, {"model": "qwen", "messages": []})
        h = seen[0]["headers"]
        assert h["x-inferweave-client-id"] == "herdr:proj-a"
        assert h["x-inferweave-session-id"].startswith("proj-a:")  # not the shared "default" bucket
        assert "x-inferweave-agent-id" not in h
    finally:
        _teardown(proxy, srvs)


def test_valid_session_and_agent_pass_through_and_client_is_not_spoofable():
    repo, key, proxy, seen, _, srvs = _setup()
    try:
        _post_headers(proxy, key, {"model": "qwen", "messages": []},
                      {"X-InferWeave-Session-ID": "mission-9", "X-InferWeave-Agent-ID": "implementer",
                       "X-InferWeave-Client-ID": "someone-else"})
        h = seen[0]["headers"]
        assert h["x-inferweave-session-id"] == "mission-9"
        assert h["x-inferweave-agent-id"] == "implementer"
        assert h["x-inferweave-client-id"] == "herdr:proj-a"
    finally:
        _teardown(proxy, srvs)


def test_x_session_id_alias_is_honoured_and_unsafe_values_are_dropped_not_forwarded():
    repo, key, proxy, seen, _, srvs = _setup()
    try:
        _post_headers(proxy, key, {"model": "qwen", "messages": []}, {"X-Session-Id": "alias-1"})
        assert seen[0]["headers"]["x-inferweave-session-id"] == "alias-1"
        _post_headers(proxy, key, {"model": "qwen", "messages": []},
                      {"X-InferWeave-Session-ID": "has space", "X-InferWeave-Agent-ID": "x" * 129})
        h = seen[1]["headers"]
        assert h["x-inferweave-session-id"].startswith("proj-a:")  # a bad value must not 400 the request
        assert "x-inferweave-agent-id" not in h
    finally:
        _teardown(proxy, srvs)


def test_per_project_inflight_cap_returns_429_with_retry_after_and_does_not_forward():
    repo, key, proxy, seen, _, srvs = _setup()
    try:
        state = proxy.RequestHandlerClass.state
        state.configure_limits(max_inflight=1, wait_seconds=0.05)
        assert state.acquire("proj-a")
        try:
            _post_headers(proxy, key, {"model": "qwen", "messages": []})
            raise AssertionError("expected 429")
        except urllib.error.HTTPError as e:
            assert e.code == 429
            assert int(e.headers["Retry-After"]) >= 1
            assert json.loads(e.read())["error"]["type"] == "proxy_backpressure"
        assert seen == []
        state.release("proj-a")
        assert _post_headers(proxy, key, {"model": "qwen", "messages": []})[0] == 200
        assert state.inflight("proj-a") == 0  # the slot is released after the request
    finally:
        _teardown(proxy, srvs)
