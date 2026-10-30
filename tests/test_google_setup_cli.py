"""herdr-eng google: status, set-client, connect, disconnect against a fake control plane."""
import io
import json
import urllib.error

import pytest

from herdr_engineering import google_setup_cli as g

REDIRECT = "https://dash.example.test" + g.CALLBACK_PATH


class Resp(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Fake:
    """The assistant-apps routes: records the calls, answers from a small state."""
    def __init__(self, client=False, connected=False, ready=True, callback=302):
        self.calls, self.client, self.connected, self.ready, self.callback = [], client, connected, ready, callback

    def __call__(self, req, timeout):
        path, method = req.full_url.split("test", 1)[1], req.get_method()
        body = json.loads(req.data) if req.data else None
        self.calls.append((method, path, body, req.headers.get("X-api-key")))
        if (method, path) == ("GET", "/api/v1/assistant-apps"):
            providers = [{"provider": "google", "name": "Gmail", "client_configured": self.client,
                          "connected": self.connected, "account": "me@example.test",
                          "status": "connected" if self.connected else "not_connected"}]
            return Resp(200, {"ready": self.ready, "redirect_uri": REDIRECT, "providers": providers})
        if path == g.CALLBACK_PATH:
            if self.callback >= 400:
                raise urllib.error.HTTPError(req.full_url, self.callback, "x", {}, io.BytesIO(b""))
            return Resp(self.callback, {})
        if (method, path) == ("PUT", "/api/v1/assistant-apps/google/client"):
            self.client = True
            return Resp(200, {"ok": True})
        if (method, path) == ("POST", "/api/v1/assistant-apps/google/connect"):
            if not self.client:
                raise urllib.error.HTTPError(req.full_url, 409, "x", {}, io.BytesIO(b'{"error":"no_client"}'))
            return Resp(200, {"url": "https://accounts.google.com/o/oauth2/v2/auth?state=s"})
        if (method, path) == ("DELETE", "/api/v1/assistant-apps/google"):
            self.connected = False
            return Resp(200, {"ok": True})
        raise AssertionError((method, path))


def api(fake, key=None):
    return g.Api("https://dash.example.test", key, opener=fake)


def test_status_lists_what_is_missing_and_the_console_steps():
    out = []
    assert g.cmd_status(api(Fake()), out.append) == 1
    text = "\n".join(out)
    assert "FIX  no OAuth client stored yet" in text and "FIX  no Google account is connected yet" in text
    assert REDIRECT in text and "Test users" in text and "In production" in text


def test_status_is_clean_when_everything_is_set_up():
    out = []
    assert g.cmd_status(api(Fake(client=True, connected=True)), out.append) == 0
    assert not any(line.startswith("FIX") for line in out)


def test_status_flags_a_dead_callback_route():
    out = []
    assert g.cmd_status(api(Fake(client=True, connected=True, callback=404)), out.append) == 1
    assert any("callback route answers (404)" in line and line.startswith("FIX") for line in out)


def test_set_client_reads_googles_json_and_never_prints_the_secret(tmp_path):
    f = tmp_path / "client_secret_x.json"
    f.write_text(json.dumps({"web": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "SECRET-VALUE",
                                     "redirect_uris": ["https://elsewhere.example.test/cb"]}}))
    fake, out = Fake(), []
    assert g.cmd_set_client(api(fake), str(f), None, None, out.append) == 0
    put = [c for c in fake.calls if c[0] == "PUT"][0]
    assert put[2] == {"client_id": "cid.apps.googleusercontent.com", "client_secret": "SECRET-VALUE"}
    text = "\n".join(out)
    assert "SECRET-VALUE" not in text
    assert "warning" in text and REDIRECT in text           # the registered redirect URI does not match


def test_set_client_refuses_a_desktop_client(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}))
    with pytest.raises(g.SetupError, match="Desktop"):
        g.cmd_set_client(api(Fake()), str(f), None, None, lambda *_: None)


def test_set_client_from_a_secret_file_and_the_hidden_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(g.sys.stdin, "isatty", lambda: True, raising=False)
    s = tmp_path / "secret"
    s.write_text("FILE-SECRET\n")
    fake = Fake()
    g.cmd_set_client(api(fake), None, "cid", str(s), lambda *_: None)
    assert [c for c in fake.calls if c[0] == "PUT"][0][2]["client_secret"] == "FILE-SECRET"
    with pytest.raises(g.SetupError, match="--json-file"):
        g.cmd_set_client(api(Fake()), None, None, None, lambda *_: None, prompt=lambda *_: "")


def test_connect_needs_a_client_first_then_prints_the_link():
    with pytest.raises(g.SetupError, match="set-client"):
        g.cmd_connect(api(Fake()), lambda *_: None)
    out = []
    assert g.cmd_connect(api(Fake(client=True)), out.append) == 0
    assert any(line.startswith("https://accounts.google.com/") for line in out)


def test_disconnect_needs_yes():
    fake = Fake(client=True, connected=True)
    with pytest.raises(g.SetupError, match="--yes"):
        g.cmd_disconnect(api(fake), False, lambda *_: None)
    assert not any(c[0] == "DELETE" for c in fake.calls)
    assert g.cmd_disconnect(api(fake), True, lambda *_: None) == 0


def test_api_key_is_sent_only_when_given():
    fake = Fake()
    api(fake, "KEY").call("GET", "/api/v1/assistant-apps")
    api(fake).call("GET", "/api/v1/assistant-apps")
    assert [c[3] for c in fake.calls] == ["KEY", None]
