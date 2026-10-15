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
            seen.append({"auth": self.headers.get("Authorization"), "body": body})
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
