"""`herdr-eng collab agents list|renew`: the real CLI against a real CollabService behind the real operator listener."""
import json
import os
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering import cli
from herdr_engineering.collab_agents_client import main as agents_main
from tests.control_plane.test_collab_operator import call
from tests.control_plane.test_rooms_bootstrap import BASE, _stack


@pytest.fixture
def stack(tmp_path):
    yield from _stack(tmp_path)


@pytest.fixture
def short_stack(tmp_path):
    yield from _stack(tmp_path, remote_credential_ttl=2.0)


@pytest.fixture
def capped_stack(tmp_path):
    yield from _stack(tmp_path, remote_credential_ttl=3.0, max_agents_per_user=1)


def connect(stack, name, harness="tui-smoke"):
    s, info = call(stack.op, "POST", f"{BASE}/agents/connect", {"harness_type": harness, "name": name},
                   host=f"127.0.0.1:{stack.op}")
    assert s == 201
    return info


# The size limit is set inside the child (never via preexec_fn, which can deadlock with server threads alive in the parent).
CHILD_CODE = ("import resource, sys; resource.setrlimit(resource.RLIMIT_FSIZE, (0, resource.getrlimit(resource.RLIMIT_FSIZE)[1])); "
              "from herdr_engineering.cli import main; sys.exit(main(sys.argv[1:]))")


def run(stack, *argv):
    return agents_main([*argv, "--operator-url", stack.url])


def test_list_prints_the_fields_and_never_a_credential(stack, capsys):
    info = connect(stack, "lister")
    assert run(stack, "list") == 0
    out = capsys.readouterr().out
    assert info["credential"] not in out
    line = next(ln for ln in out.splitlines() if info["agent_id"] in ln)
    assert "lister" in line and "tui-smoke" in line and "active" in line and "yes" in line


def test_list_json_has_only_the_listed_fields(stack, capsys):
    info = connect(stack, "lister")
    assert run(stack, "list", "--json") == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    row = next(r for r in rows if r["agent_id"] == info["agent_id"])
    assert set(row) == {"agent_id", "name", "harness_type", "status", "expires_at", "renewable"}
    assert info["credential"] not in out


def test_renew_writes_the_credential_only_to_a_0600_file(short_stack, tmp_path, capsys):
    stack = short_stack
    info = connect(stack, "keeper")
    dest = tmp_path / "cred"
    assert run(stack, "renew", info["agent_id"], "--credential-file", str(dest)) == 0
    out = capsys.readouterr()
    new = dest.read_text().strip()
    assert new.startswith("hca_") and new != info["credential"]
    assert new not in out.out + out.err and info["credential"] not in out.out + out.err
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600
    assert info["agent_id"] in out.out and "grants_restored" in out.out and "expires_at" in out.out


def test_renew_by_name(stack, tmp_path, capsys):
    info = connect(stack, "byname")
    dest = tmp_path / "cred"
    assert run(stack, "renew", "@byname", "--credential-file", str(dest)) == 0
    assert info["agent_id"] in capsys.readouterr().out and dest.read_text().startswith("hca_")


def test_unknown_name_is_a_clear_error_and_creates_no_file(stack, tmp_path, capsys):
    dest = tmp_path / "cred"
    assert run(stack, "renew", "@nobody", "--credential-file", str(dest)) == 1
    assert "no agent named" in capsys.readouterr().err and not dest.exists()


def test_existing_file_is_refused_without_force_and_the_agent_is_not_renewed(stack, tmp_path, capsys):
    info = connect(stack, "guarded")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dest = out_dir / "cred"
    dest.write_text("keep me")
    assert run(stack, "renew", info["agent_id"], "--credential-file", str(dest)) == 1
    assert "--force" in capsys.readouterr().err and dest.read_text() == "keep me"
    assert run(stack, "renew", info["agent_id"], "--credential-file", str(dest), "--force") == 0
    assert dest.read_text().startswith("hca_") and stat.S_IMODE(os.stat(dest).st_mode) == 0o600
    assert [p.name for p in out_dir.iterdir()] == ["cred"]       # no temp file left behind


def test_print_credential_is_explicit_and_goes_to_stdout_only(stack, capsys):
    info = connect(stack, "shown")
    assert run(stack, "renew", info["agent_id"], "--print-credential") == 0
    out = capsys.readouterr()
    assert "hca_" in out.out and "hca_" not in out.err


def agent_authenticates(stack, info, credential=None):
    """Whether the public listener accepts the credential (any answer but 401; this route is just switched off for agents)."""
    headers = {"Authorization": f"Agent {credential or info['credential']}", "X-Herdr-Device": info["device_id"],
               "X-Forwarded-Proto": "https"}
    return call(stack.public, "GET", f"{BASE}/delegations/inbox", headers=headers)[0] != 401


def test_renew_without_a_destination_is_refused_before_any_request(stack, capsys):
    info = connect(stack, "nodest")
    assert agent_authenticates(stack, info)
    assert run(stack, "renew", info["agent_id"]) == 2
    assert "--credential-file" in capsys.readouterr().err
    assert agent_authenticates(stack, info)            # the original credential still authenticates: nothing was renewed


def agents_json(capsys):
    return json.loads(capsys.readouterr().out)


def test_renew_json_output_has_no_credential_unless_printed(stack, tmp_path, capsys):
    info = connect(stack, "jsoned")
    dest = tmp_path / "cred"
    assert run(stack, "renew", info["agent_id"], "--credential-file", str(dest), "--json") == 0
    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["agent_id"] == info["agent_id"] and "expires_at" in body and "grants_restored" in body
    assert "credential" not in body and dest.read_text().strip() not in out


def test_revoked_agent_exits_nonzero_with_409_message_and_leaves_no_file(stack, tmp_path, capsys):
    info = connect(stack, "gone")
    host = f"127.0.0.1:{stack.op}"
    assert call(stack.op, "POST", f"{BASE}/agents/{info['agent_id']}/revoke", {}, host=host)[0] == 200
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dest = out_dir / "cred"
    assert run(stack, "renew", info["agent_id"], "--credential-file", str(dest)) == 1
    err = capsys.readouterr().err
    assert "409" in err and "revoked" in err and not dest.exists()
    assert not list(out_dir.iterdir())


def test_unknown_id_is_404(stack, tmp_path, capsys):
    dest = tmp_path / "cred"
    assert run(stack, "renew", "no-such-agent-id", "--credential-file", str(dest)) == 1
    assert "404" in capsys.readouterr().err and not dest.exists()


def test_unreachable_operator_is_a_clear_error(tmp_path, capsys):
    assert agents_main(["list", "--operator-url", "http://127.0.0.1:9"]) == 1
    assert "unreachable" in capsys.readouterr().err


def test_registered_in_the_top_level_cli(stack, tmp_path, capsys):
    info = connect(stack, "wired")
    assert cli.main(["collab", "agents", "list", "--operator-url", stack.url, "--json"]) == 0
    assert any(r["agent_id"] == info["agent_id"] for r in agents_json(capsys))


def test_a_directory_target_is_refused_before_renewing_even_with_force(stack, tmp_path, capsys):
    info = connect(stack, "dirtarget")
    target = tmp_path / "adir"
    target.mkdir()
    assert run(stack, "renew", info["agent_id"], "--credential-file", str(target), "--force") == 1
    assert "directory" in capsys.readouterr().err
    assert agent_authenticates(stack, info)


def test_a_failed_write_after_renewing_hands_the_credential_to_stderr_with_a_hint(stack, tmp_path):
    """The real CLI in a process that may not write any file bytes (EFBIG): the old credential is dead, so the new one must survive."""
    info = connect(stack, "diskfull")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dest = out_dir / "cred"
    code = CHILD_CODE
    done = subprocess.run([sys.executable, "-B", "-c", code, "collab", "agents", "renew", info["agent_id"],
                           "--credential-file", str(dest), "--operator-url", stack.url],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "Traceback" not in done.stderr
    recovered = [w for w in done.stderr.split() if w.startswith("hca_")]
    assert len(recovered) == 1 and recovered[0] != info["credential"] and recovered[0] not in done.stdout
    assert "only copy" in done.stderr and not list(out_dir.iterdir())
    assert not agent_authenticates(stack, info)
    assert agent_authenticates(stack, info, recovered[0])


def test_helper_style_refusal_is_403(stack, tmp_path, capsys):
    info = connect(stack, "fabricated")
    host = f"127.0.0.1:{stack.op}"
    assert call(stack.op, "POST", f"{BASE}/agents/{info['agent_id']}/fabric", {"fabric": True, "machine": "bender"},
                host=host)[0] == 200
    dest = tmp_path / "cred"
    assert run(stack, "renew", info["agent_id"], "--credential-file", str(dest)) == 1
    assert "403" in capsys.readouterr().err and not dest.exists()


def test_agent_cap_is_429(capped_stack, tmp_path, capsys):
    stk = capped_stack
    info = connect(stk, "first")
    time.sleep(3.6)                                    # the first agent expires and frees its slot
    connect(stk, "second")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dest = out_dir / "cred"
    assert run(stk, "renew", info["agent_id"], "--credential-file", str(dest)) == 1
    assert "429" in capsys.readouterr().err and not list(out_dir.iterdir())


class _Garbage(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = b"<html>not json"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.do_POST()


def test_a_bad_renew_answer_says_the_outcome_is_unknown_not_unreachable(tmp_path, capsys):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Garbage)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        dest = tmp_path / "cred"
        assert agents_main(["renew", "some-agent", "--credential-file", str(dest), "--operator-url", url]) == 1
        err = capsys.readouterr().err
        assert "outcome unknown" in err and "re-run" in err and "unreachable" not in err and not dest.exists()
        assert agents_main(["list", "--operator-url", url]) == 1               # a malformed list is a message, not a traceback
        assert "unreachable" not in capsys.readouterr().err
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://127.0.0.1/", "http://example.com:9482"])
def test_operator_url_must_be_http_loopback_or_https(url, capsys):
    assert agents_main(["list", "--operator-url", url]) == 1
    assert "operator-url" in capsys.readouterr().err


def test_a_failed_write_with_print_credential_prints_it_on_stdout_not_stderr(stack, tmp_path):
    info = connect(stack, "diskfull2")
    done = subprocess.run([sys.executable, "-B", "-c", CHILD_CODE, "collab", "agents", "renew", info["agent_id"],
                           "--credential-file", str(tmp_path / "cred"), "--print-credential", "--operator-url", stack.url],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "Traceback" not in done.stderr
    printed = done.stdout.split()
    assert len(printed) == 1 and printed[0].startswith("hca_") and printed[0] != info["credential"]
    assert "hca_" not in done.stderr and "stdout" in done.stderr
    assert agent_authenticates(stack, info, printed[0])


def test_json_with_print_credential_is_one_json_object_carrying_the_credential(stack, capsys):
    info = connect(stack, "jsoncred")
    assert run(stack, "renew", info["agent_id"], "--print-credential", "--json") == 0
    out = capsys.readouterr().out
    body = json.loads(out)                                   # the whole stdout is exactly one JSON document
    assert body["credential"].startswith("hca_") and body["agent_id"] == info["agent_id"]
    assert len(out.strip().splitlines()) == 1
    assert agent_authenticates(stack, info, body["credential"])


def test_a_malformed_operator_url_is_a_one_line_error(capsys):
    assert agents_main(["list", "--operator-url", "http://[::1"]) == 1
    err = capsys.readouterr().err
    assert "operator-url" in err and "Traceback" not in err and len(err.strip().splitlines()) == 1


class _SlowErrorBody(BaseHTTPRequestHandler):
    """Sends an error status whose body never arrives, so reading the detail times out."""

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(500)
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.flush()
        time.sleep(2.5)


def test_a_timeout_reading_an_error_body_is_a_clean_error(capsys, monkeypatch):
    from herdr_engineering import collab_agents_client as client
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SlowErrorBody)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real = client._call
    monkeypatch.setattr(client, "_call", lambda base, method, path, timeout=0.5: real(base, method, path, 0.5))
    try:
        assert agents_main(["list", "--operator-url", f"http://127.0.0.1:{srv.server_port}"]) == 1
        err = capsys.readouterr().err
        assert "HTTP 500" in err and "Traceback" not in err
    finally:
        srv.shutdown()
        srv.server_close()


class _NoIdRow(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        body = b'{"agents": [{"name": "idless", "status": "active"}]}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_a_listed_agent_without_an_id_is_a_clean_error(tmp_path, capsys):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _NoIdRow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        dest = tmp_path / "cred"
        assert agents_main(["renew", "@idless", "--credential-file", str(dest),
                            "--operator-url", f"http://127.0.0.1:{srv.server_port}"]) == 1
        err = capsys.readouterr().err
        assert "no agent id" in err and "Traceback" not in err and not dest.exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_failed_move_into_place_keeps_the_0600_temp_file_and_prints_its_path(tmp_path, capsys):
    """os.replace fails for real: the destination became a directory after the temp file was reserved."""
    from herdr_engineering.collab_agents_client import _reserve, _store
    dest = tmp_path / "cred"
    tmp, fd = _reserve(str(dest), False)
    dest.mkdir()
    assert _store("hca_secretvalue", tmp, fd, str(dest)) is False
    err = capsys.readouterr().err
    assert tmp in err and "hca_secretvalue" not in err and "0600" in err
    assert stat.S_IMODE(os.stat(tmp).st_mode) == 0o600
    assert open(tmp).read() == "hca_secretvalue\n"
