"""Per-project LLM env injection (mocked control-plane API)."""
import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from herdr_engineering import session_env as se
from herdr_engineering.cli import main

SECRET = "hk-super-secret"


class _CP:
    """A fake control plane recording calls; mints sequential keys."""

    def __init__(self, require_key=None):
        self.calls, self.keys, self.n = [], {}, 0
        cp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                out = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def _ok(self):
                if require_key and self.headers.get("X-API-Key") != require_key:
                    self._send(401, {"error": "auth"})
                    return False
                return True

            def do_GET(self):  # noqa: N802
                cp.calls.append(("GET", self.path))
                if self._ok():
                    self._send(200, {"keys": [{"key_id": k, "revoked": r}
                                              for k, r in cp.keys.items()]})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                cp.calls.append(("POST", self.path, body))
                if not self._ok():
                    return
                if self.path == "/api/v1/llm/keys":
                    cp.n += 1
                    cp.keys[f"k{cp.n}"] = False
                    self._send(200, {"key_id": f"k{cp.n}", "project": body["project"],
                                     "key": f"{SECRET}-{cp.n}"})
                elif self.path.endswith("/revoke"):
                    cp.keys[self.path.split("/")[-2]] = True
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {})

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def cp(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    c = _CP()
    yield c
    c.close()


def _cfg(cp, **llm):
    return {"llm": {"control_plane_url": cp.url, "gateway_url": "http://dev.lan:8088/v1", **llm}}


def _dump_env_cmd(out):
    code = ("import json,os,sys;"
            "json.dump({k:v for k,v in os.environ.items() if k.startswith(('OPENAI','HERDR'))},"
            f"open({str(out)!r},'w'))")
    return [sys.executable, "-c", code]


def test_session_gets_env_not_argv_and_key_is_revoked_after(cp, tmp_path, capfd):
    out = tmp_path / "env.json"
    cmd = _dump_env_cmd(out)
    rc = se.run_session(_cfg(cp), project="proj", command=cmd, session="s1", mission="m1")
    assert rc == 0
    env = json.loads(out.read_text())
    assert env["OPENAI_BASE_URL"] == "http://dev.lan:8088/v1"
    assert env["OPENAI_API_KEY"] == f"{SECRET}-1"
    assert env["HERDR_SESSION_ID"] == "s1" and env["HERDR_PROJECT"] == "proj"
    assert env["HERDR_ENGINEERING_CONTROL_PLANE_URL"] == cp.url
    assert SECRET not in " ".join(cmd)
    assert cp.keys == {"k1": True}                    # revoked at session end
    mint = [c for c in cp.calls if c[1] == "/api/v1/llm/keys" and c[0] == "POST"][0]
    assert mint[2] == {"project": "proj", "label": "session:s1"}
    cap = capfd.readouterr()
    assert SECRET not in cap.out + cap.err
    assert "OPENAI_API_KEY" not in os.environ


def test_key_revoked_even_when_command_fails(cp):
    rc = se.run_session(_cfg(cp), project="p", command=[sys.executable, "-c", "raise SystemExit(7)"])
    assert rc == 7 and cp.keys == {"k1": True}


@pytest.mark.parametrize("how", ["flag", "config"])
def test_opt_out_mints_nothing(cp, tmp_path, monkeypatch, how):
    out = tmp_path / "env.json"
    cfg = _cfg(cp, inject=(how != "config"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = se.run_session(cfg, project="p", command=_dump_env_cmd(out), disable=(how == "flag"))
    assert rc == 0 and cp.keys == {}
    assert "OPENAI_API_KEY" not in json.loads(out.read_text())


def test_project_mode_reuses_active_key_and_never_revokes(cp, tmp_path):
    cfg = _cfg(cp, key_mode="project")
    out = tmp_path / "e.json"
    se.run_session(cfg, project="p", command=_dump_env_cmd(out))
    se.run_session(cfg, project="p", command=_dump_env_cmd(out))
    assert cp.n == 1 and cp.keys == {"k1": False}
    cache = tmp_path / ".config/herdr-engineering/llm-p.env"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    cp.keys["k1"] = True                              # key revoked elsewhere -> rotate
    se.run_session(cfg, project="p", command=_dump_env_cmd(out))
    assert cp.n == 2 and json.loads(out.read_text())["OPENAI_API_KEY"] == f"{SECRET}-2"


def test_project_cache_with_loose_permissions_is_not_trusted(cp, tmp_path):
    cfg = _cfg(cp, key_mode="project")
    cache = tmp_path / ".config/herdr-engineering/llm-p.env"
    cache.parent.mkdir(parents=True)
    cache.write_text("OPENAI_API_KEY=stolen\nHERDR_LLM_KEY_ID=k1\n")
    cache.chmod(0o644)
    out = tmp_path / "e.json"
    se.run_session(cfg, project="p", command=_dump_env_cmd(out))
    assert json.loads(out.read_text())["OPENAI_API_KEY"] == f"{SECRET}-1"


def test_unreachable_control_plane_fails_open_without_leaking(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = {"llm": {"control_plane_url": "http://127.0.0.1:9"}}
    out = tmp_path / "e.json"
    assert se.run_session(cfg, project="p", command=_dump_env_cmd(out)) == 0
    assert "OPENAI_API_KEY" not in json.loads(out.read_text())
    assert "not injected" in capfd.readouterr().err
    assert se.run_session(cfg, project="p", command=_dump_env_cmd(out), require=True) == 3


def test_api_key_header_sent_and_redirects_not_followed(tmp_path, monkeypatch, fake_host_config):
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_host_config({"ingest": {"api_key": "operator-key"}})
    c = _CP(require_key="operator-key")
    try:
        assert se.mint_key(c.url, "p")["key_id"] == "k1"
    finally:
        c.close()
    # a redirect (which would forward X-API-Key elsewhere) is refused
    class R(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:9/x")
            self.end_headers()
    srv = HTTPServer(("127.0.0.1", 0), R)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(se.LlmEnvError):
            se.mint_key(f"http://127.0.0.1:{srv.server_address[1]}", "p")
    finally:
        srv.shutdown()


def test_env_file_is_0600_even_over_a_loose_existing_file(tmp_path):
    f = tmp_path / "x.env"
    f.write_text("old")
    f.chmod(0o644)
    se.write_env_file(str(f), {"OPENAI_API_KEY": SECRET})
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    with pytest.raises(se.LlmEnvError):
        se.write_env_file(str(f), {"A": "x\nB=y"})


def test_cli_session_run_end_to_end(cp, tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_CONFIG", str(tmp_path / "c.yaml"))
    (tmp_path / "c.yaml").write_text(f"version: 1\nllm:\n  control_plane_url: {cp.url}\n")
    out = tmp_path / "e.json"
    rc = main(["session", "run", "--project", "p", "--session", "s", "--"] + _dump_env_cmd(out))
    assert rc == 0 and json.loads(out.read_text())["OPENAI_API_KEY"] == f"{SECRET}-1"
    assert cp.keys == {"k1": True}
    assert main(["session", "run", "--project", "p", "--"]) == 2
