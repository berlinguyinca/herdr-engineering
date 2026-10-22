"""An empty api key (the `init` default: the control plane is open on the tailnet)
must mean NO X-API-Key header at all - never an empty one."""
import io
import json

from herdr_engineering import session_env
from herdr_engineering.mission_activity_feed import MissionActivityFeed
from herdr_engineering.service_metadata_feed import MetadataClient


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return _Resp(json.dumps({"ok": True}).encode())


def _has_key_header(request):
    return any(k.lower() == "x-api-key" for k, _ in request.header_items())


def test_helper_detects_a_key_header(tmp_path):
    feed = MissionActivityFeed("http://cp.test", host_id="h", control_dir=tmp_path, api_key="k")
    feed._http = _Opener()
    feed._http_json("/api/v1/ingest", {"x": 1})
    assert _has_key_header(feed._http.requests[0])


def test_mission_feed_sends_no_header_for_empty_key(tmp_path):
    feed = MissionActivityFeed("http://cp.test", host_id="h", control_dir=tmp_path, api_key="")
    feed._http = _Opener()
    feed._http_json("/api/v1/ingest", {"x": 1})
    assert not _has_key_header(feed._http.requests[0])
    feed.retarget(None, "")          # a refresh to an empty key also sends none
    feed._http_json("/api/v1/ingest", {"x": 1})
    assert not _has_key_header(feed._http.requests[1])


def test_metadata_client_sends_no_header_for_empty_key():
    client = MetadataClient("http://cp.test", api_key="")
    client.http = _Opener()
    client.request("/api/v1/ingest", {"x": 1})
    assert not _has_key_header(client.http.requests[0])


def test_session_env_sends_no_header_for_empty_agent_key(monkeypatch, tmp_path):
    import herdr_engineering.host_config as hc
    monkeypatch.setattr(hc, "host_value", lambda key, default=None: "")
    monkeypatch.setattr(session_env, "KEY_FILE", str(tmp_path / "missing-key"))
    opener = _Opener()
    monkeypatch.setattr(session_env, "_OPENER", opener)
    session_env._post("http://cp.test", "/api/v1/llm/keys", {"project": "p"})
    session_env._get("http://cp.test", "/api/v1/llm/config")
    assert opener.requests and not any(_has_key_header(r) for r in opener.requests)
