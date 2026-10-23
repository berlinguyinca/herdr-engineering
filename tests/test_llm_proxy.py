import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

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


# Policy routing uses the catalog of the configured fabric, never caller URLs.


def _gpu_model(**changes):
    return {"id": "gpu/qwen-small", "aliases": ["qwen-small"], "x_state": "hot",
            "slots": 1, "ctx_per_request": 8192, "x_modality": "text", **changes}


def _policy_setup(catalog=None, gpu_status=200, catalog_status=200):
    repo, key, proxy, seen, cpu, srvs = _setup()
    state = proxy.RequestHandlerClass.state
    custom = {"name": "summary", "base_url": f"http://127.0.0.1:{srvs[1].server_address[1]}",
              "upstream_model": "cpu-small", "fabric_model": "qwen-small",
              "context_window": 8192, "project": "proj-a"}
    original_config = state.control.config
    state.control.config = lambda: {**original_config(), "custom_models": [custom]}
    state.refresh(force=True)
    catalog_calls = []
    handler = srvs[0].RequestHandlerClass
    original_post = handler.do_POST

    def get(self):
        catalog_calls.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(catalog_status)
        self.end_headers()
        self.wfile.write(json.dumps({"data": catalog if catalog is not None else [_gpu_model()]}).encode())

    def post(self):
        if gpu_status == 200:
            return original_post(self)
        self.rfile.read(int(self.headers["Content-Length"]))
        seen.append({"status": gpu_status})
        if gpu_status == -1:
            self.close_connection = True
            return
        self.send_response(gpu_status)
        self.end_headers()
        self.wfile.write(b'{"error":"capacity"}')

    handler.do_GET = get
    handler.do_POST = post
    return repo, key, proxy, seen, cpu, srvs, catalog_calls


def _summary_post(proxy, key, **body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_address[1]}/v1/chat/completions",
        data=json.dumps({"model": "summary", "messages": [], **body}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "X-Herdr-Require-Registered-Model": "true", "X-InferWeave-Session-ID": "summary-session"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.status, dict(response.headers), response.read()


def test_registered_policy_routes_exact_alias_to_concrete_fabric_model_and_preserves_accounting():
    repo, key, proxy, gpu, cpu, srvs, calls = _policy_setup()
    try:
        status, headers, _ = _summary_post(proxy, key)
        assert status == 200 and headers["X-Herdr-LLM-Route"] == "fabric"
        assert gpu[0]["body"]["model"] == "gpu/qwen-small"
        assert gpu[0]["auth"] == "Bearer UPSTREAM-SECRET"
        assert gpu[0]["headers"]["x-inferweave-session-id"] == "summary-session"
        assert "x-herdr-require-registered-model" not in gpu[0]["headers"]
        assert calls[0]["authorization"] == "Bearer UPSTREAM-SECRET"
        assert cpu == []
        assert repo.token_analytics()["by_model"]["summary"]["requests"] == 1
        assert proxy.RequestHandlerClass.state.inflight("proj-a") == 0
    finally:
        _teardown(proxy, srvs)


@pytest.mark.parametrize("catalog", [[], [_gpu_model(x_state="cold")], [_gpu_model(slots=0)],
    [_gpu_model(ctx_per_request=4096)], [_gpu_model(x_group=True)], [_gpu_model(x_modality="embedding")],
    [_gpu_model(aliases=["other-model"])], [_gpu_model(), _gpu_model(id="gpu/ambiguous")],
    [_gpu_model(slots=True)], [_gpu_model(ctx_per_request=None)], [{"id": "qwen-small"}]])
def test_ineligible_fabric_catalog_falls_back_only_to_registered_cpu(catalog):
    repo, key, proxy, gpu, cpu, srvs, _ = _policy_setup(catalog)
    try:
        status, headers, _ = _summary_post(proxy, key)
        assert status == 200 and headers["X-Herdr-LLM-Route"] == "cpu"
        assert gpu == [] and cpu[0]["body"]["model"] == "cpu-small"
        assert cpu[0]["auth"] is None
        assert "x-herdr-require-registered-model" not in cpu[0]["headers"]
    finally:
        _teardown(proxy, srvs)


@pytest.mark.parametrize("changes", [{"id": "qwen-small", "aliases": []},
                                      {"x_state": "warm", "ctx_per_request": None, "x_context_window": 8192}])
def test_exact_id_and_warm_context_window_catalog_are_eligible(changes):
    _, key, proxy, gpu, cpu, srvs, _ = _policy_setup([_gpu_model(**changes)])
    try:
        _summary_post(proxy, key)
        assert len(gpu) == 1 and cpu == []
    finally:
        _teardown(proxy, srvs)


@pytest.mark.parametrize("gpu_status", [429, 503, -1])
def test_pre_response_gpu_capacity_or_connection_failure_uses_cpu_once(gpu_status):
    repo, key, proxy, gpu, cpu, srvs, _ = _policy_setup(gpu_status=gpu_status)
    try:
        status, headers, _ = _summary_post(proxy, key)
        assert status == 200 and headers["X-Herdr-LLM-Route"] == "cpu"
        assert len(gpu) == len(cpu) == 1
        assert cpu[0]["body"]["model"] == "cpu-small" and cpu[0]["auth"] is None
        assert repo.token_analytics()["by_model"]["summary"]["requests"] == 1
    finally:
        _teardown(proxy, srvs)


def test_catalog_failure_fails_closed_and_cache_expires():
    _, key, proxy, gpu, cpu, srvs, calls = _policy_setup(catalog_status=503)
    try:
        _summary_post(proxy, key)
        _summary_post(proxy, key)
        assert len(calls) == 1 and len(cpu) == 2 and gpu == []
        proxy.RequestHandlerClass.state._catalog_last -= 11
        _summary_post(proxy, key)
        assert len(calls) == 2
    finally:
        _teardown(proxy, srvs)


def test_guard_rejects_missing_registration_and_foreign_project_before_dispatch():
    repo, key, proxy, gpu, cpu, srvs, calls = _policy_setup()
    try:
        with pytest.raises(urllib.error.HTTPError) as missing:
            _summary_post(proxy, key, model="missing")
        assert missing.value.code == 503
        assert json.loads(missing.value.read())["error"]["code"] == "model_not_registered"
        foreign = repo.create_project_key("proj-b")["key"]
        proxy.RequestHandlerClass.state.refresh(force=True)
        with pytest.raises(urllib.error.HTTPError) as forbidden:
            _summary_post(proxy, foreign)
        assert forbidden.value.code == 403
        assert gpu == cpu == calls == []
    finally:
        _teardown(proxy, srvs)


@pytest.mark.parametrize("status,stream", [(500, False), (429, True), (503, True)])
def test_policy_does_not_retry_application_errors_or_streaming(status, stream):
    _, key, proxy, gpu, cpu, srvs, _ = _policy_setup(gpu_status=status)
    try:
        with pytest.raises(urllib.error.HTTPError) as failure:
            _summary_post(proxy, key, stream=stream)
        assert failure.value.code == status
        assert len(gpu) == 1 and cpu == []
    finally:
        _teardown(proxy, srvs)


def test_expired_catalog_success_is_discarded_when_refresh_fails():
    _, key, proxy, gpu, cpu, srvs, calls = _policy_setup()
    try:
        _summary_post(proxy, key)
        _summary_post(proxy, key)
        assert len(calls) == 1 and len(gpu) == 2
        state = proxy.RequestHandlerClass.state
        state._catalog_last -= 11

        def unavailable(self):
            self.send_response(503)
            self.end_headers()

        srvs[0].RequestHandlerClass.do_GET = unavailable
        _summary_post(proxy, key)
        assert len(gpu) == 2 and len(cpu) == 1
        assert state._catalog == []
    finally:
        _teardown(proxy, srvs)


@pytest.mark.parametrize("payload", [b"not json", b'{"data":{}}', b'{"data":[1]}', b"x" * (1024 * 1024 + 1)])
def test_malformed_or_oversized_catalog_fails_closed(payload):
    _, key, proxy, gpu, cpu, srvs, _ = _policy_setup()
    try:
        def invalid(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        srvs[0].RequestHandlerClass.do_GET = invalid
        _summary_post(proxy, key)
        assert len(cpu) == 1 and gpu == []
    finally:
        _teardown(proxy, srvs)


def test_registration_project_scope_applies_without_guard_header_too():
    repo, _, proxy, gpu, cpu, srvs, calls = _policy_setup()
    try:
        foreign = repo.create_project_key("proj-b")["key"]
        proxy.RequestHandlerClass.state.refresh(force=True)
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(proxy, foreign, {"model": "summary"})
        assert error.value.code == 403
        assert gpu == cpu == calls == []
    finally:
        _teardown(proxy, srvs)


def test_catalog_redirect_never_receives_upstream_bearer():
    _, key, proxy, gpu, cpu, srvs, _ = _policy_setup()
    leaked = []
    try:
        def redirect(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{srvs[1].server_address[1]}/steal")
            self.end_headers()

        def capture(self):
            leaked.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":[]}')

        srvs[0].RequestHandlerClass.do_GET = redirect
        srvs[1].RequestHandlerClass.do_GET = capture
        _summary_post(proxy, key)
        assert leaked == [] and gpu == [] and len(cpu) == 1
    finally:
        _teardown(proxy, srvs)


@pytest.mark.parametrize("chunked", [False, True])
def test_truncated_fabric_response_fails_accounting_without_cpu_retry(chunked):
    repo, key, proxy, gpu, cpu, srvs, _ = _policy_setup()
    try:
        def truncated(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            gpu.append({"status": 200})
            self.send_response(200)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", "5000")
            self.end_headers()
            self.wfile.write(b'100\r\n{"choices":[' if chunked else b'{"choices":[')
            self.wfile.flush()
            self.close_connection = True

        srvs[0].RequestHandlerClass.do_POST = truncated
        with pytest.raises(http.client.IncompleteRead):
            _summary_post(proxy, key)
        assert len(gpu) == 1 and cpu == []
        totals = repo.token_analytics()["by_model"]["summary"]
        assert totals["requests"] == totals["failed"] == 1
        assert proxy.RequestHandlerClass.state.inflight("proj-a") == 0
    finally:
        _teardown(proxy, srvs)


def test_legacy_static_model_project_is_metadata_unless_registration_guard_requested():
    repo, _, proxy, upstream, custom, srvs = _setup()
    try:
        repo.upsert_custom_model("my-model", f"http://127.0.0.1:{srvs[1].server_address[1]}",
                                 "real-name", project="proj-a")
        foreign = repo.create_project_key("proj-b")["key"]
        proxy.RequestHandlerClass.state.refresh(force=True)
        assert _post(proxy, foreign, {"model": "my-model"})[0] == 200
        assert len(custom) == 1 and upstream == []
        with pytest.raises(urllib.error.HTTPError) as forbidden:
            _post_headers(proxy, foreign, {"model": "my-model"},
                          {"X-Herdr-Require-Registered-Model": "true"})
        assert forbidden.value.code == 403
        assert len(custom) == 1 and upstream == []
    finally:
        _teardown(proxy, srvs)
