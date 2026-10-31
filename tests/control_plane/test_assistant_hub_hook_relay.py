"""Claude Code hook relay + installer + redeem CLI. REAL HubServer over loopback; every test uses a throwaway HOME
(tmp_path) and explicit paths. Nothing here reads or writes the real ~/.claude, ~/.codex, ~/.config/herdr, ..."""
import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from herdr_engineering.control_plane.assistant_hub import hook_relay
from herdr_engineering.control_plane.assistant_hub.events import list_events
from herdr_engineering.control_plane.assistant_hub.hook_relay import (
    RelayConfig,
    RelayConfigError,
    install_hooks,
    relay,
)
from tests.control_plane import test_assistant_hub_server as hub_helpers

enrol = hub_helpers.enrol
started = hub_helpers.started  # the real-HubServer fixture, reused


def write_cfg(path: Path, srv, creds, device="dev1", mode=0o600):
    path.write_text(json.dumps({"url": f"http://127.0.0.1:{srv.port}", "credential": creds["credential"],
                                "device_id": device}))
    os.chmod(path, mode)
    return RelayConfig.load(path)


def setup(started, tmp_path):
    hub, srv = started()
    creds, _ = enrol(hub, srv.port)
    cfg = write_cfg(tmp_path / "w.json", srv, creds)
    return hub, srv, creds, cfg


PERM = {"session_id": "s1", "hook_event_name": "PermissionRequest", "tool_name": "Bash",
        "tool_input": {"command": "ls -la"}, "cwd": "/work"}


def events(hub):
    return list_events(hub.store, tenant_id="default", user_id="u1")


def test_relay_prints_nothing_for_permission_request(started, tmp_path, capsys):
    _, _, _, cfg = setup(started, tmp_path)
    assert relay(dict(PERM), cfg) is None
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_relay_posts_observation_to_real_hub(started, tmp_path):
    hub, _, creds, cfg = setup(started, tmp_path)
    ev = {"session_id": "s9", "hook_event_name": "PostToolUse", "tool_name": "Read", "tool_use_id": "tu1",
          "tool_input": {"file_path": "/etc/hosts", "limit": 5}, "cwd": "/work",
          "tool_response": {"content": "SECRET-BODY sk-abcdefghijklmnopqrstuvwxyz0123"}}
    assert relay(ev, cfg) is None
    (e,) = events(hub)
    assert e.provenance == "harness_attested" and e.kind == "observation" and e.session_id == "s9"
    assert e.payload["hook_event"] == "PostToolUse" and e.payload["tool_name"] == "Read"
    assert e.payload["tool_use_id"] == "tu1" and e.payload["outcome"] == "ran"
    assert e.payload["tool_input_summary"] == "/etc/hosts" and e.payload["cwd"] == "/work"
    assert e.payload["harness"] == "claude" and "permission_decision" not in e.payload
    blob = json.dumps(e.payload)
    assert "tool_response" not in blob and "SECRET-BODY" not in blob and "sk-abc" not in blob
    assert creds["credential"] not in blob


def test_relay_marks_failed_outcome_and_summarises_other_tools_by_keys(started, tmp_path):
    hub, _, _, cfg = setup(started, tmp_path)
    relay({"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "WebFetch",
           "tool_input": {"url": "http://x", "prompt": "p"}, "tool_response": {"is_error": True, "error": "boom"}}, cfg)
    (e,) = events(hub)
    assert e.payload["outcome"] == "failed"
    assert e.payload["tool_input_summary"] == "keys: prompt, url"


def test_relay_session_id_fallback(started, tmp_path):
    hub, _, _, cfg = setup(started, tmp_path)
    relay({"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {}}, cfg)
    assert events(hub)[0].session_id == "unknown"


def test_relay_unreachable_hub_exits_clean_no_output(tmp_path, capsys):
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"url": "http://127.0.0.1:1", "credential": "c" * 20, "device_id": "d"}))
    os.chmod(p, 0o600)
    assert relay(dict(PERM), RelayConfig.load(p)) is None
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_relay_http_error_is_swallowed(started, tmp_path):
    hub, srv, creds, _ = setup(started, tmp_path)
    bad = RelayConfig(url=f"http://127.0.0.1:{srv.port}", credential="nope", device_id="dev1")
    assert relay(dict(PERM), bad) is None
    assert events(hub) == []


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o604, 0o666])
def test_relay_refuses_world_readable_credential_file(started, tmp_path, mode):
    hub, srv = started()
    creds, _ = enrol(hub, srv.port)
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"url": f"http://127.0.0.1:{srv.port}", "credential": creds["credential"],
                             "device_id": "dev1"}))
    os.chmod(p, mode)
    with pytest.raises(RelayConfigError):
        RelayConfig.load(p)
    # and the entry point sends nothing
    env = {"HERDR_ASSISTANT_WORKER_FILE": str(p), "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-I", hook_relay.__file__], input=json.dumps(PERM), env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert events(hub) == []


def test_relay_refuses_symlink_and_bad_urls(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"url": "http://127.0.0.1:1", "credential": "c" * 20, "device_id": "d"}))
    os.chmod(real, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(RelayConfigError):
        RelayConfig.load(link)
    for url in ("ftp://127.0.0.1:1", "http://user:pw@127.0.0.1:1", "http://", "file:///etc/passwd", "127.0.0.1:1"):
        real.write_text(json.dumps({"url": url, "credential": "c" * 20, "device_id": "d"}))
        with pytest.raises(RelayConfigError):
            RelayConfig.load(real)
    with pytest.raises(RelayConfigError):
        RelayConfig.load(tmp_path / "missing.json")


def test_relay_writes_no_decision_files(started, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _, _, _, cfg = setup(started, tmp_path)

    def listing():
        return sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    before = listing()
    for _ in range(3):
        relay(dict(PERM), cfg)
        relay({**PERM, "hook_event_name": "PostToolUse", "tool_use_id": "t"}, cfg)
    assert listing() == before


def test_relay_truncates_and_scrubs_bash_command(started, tmp_path):
    hub, _, _, cfg = setup(started, tmp_path)
    relay({**PERM, "tool_input": {"command": "curl -H 'x: sk-abcdefghijklmnopqrstuvwxyz0123' http://h"}}, cfg)
    relay({**PERM, "tool_input": {"command": "echo " + "a" * 1000}}, cfg)
    long_e, tok_e = events(hub)
    assert "sk-abcdefghijklmnop" not in json.dumps(tok_e.payload)
    assert "curl" in tok_e.payload["tool_input_summary"]
    assert len(long_e.payload["tool_input_summary"]) <= 300
    assert long_e.payload["tool_input_summary"].startswith("echo aaa")


def test_hook_main_reads_stdin_exits_zero(started, tmp_path):
    hub, srv, _, cfg = setup(started, tmp_path)
    env = {"HERDR_ASSISTANT_WORKER_FILE": str(tmp_path / "w.json"), "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-I", hook_relay.__file__], input=json.dumps(PERM), env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert len(events(hub)) == 1
    r = subprocess.run([sys.executable, "-I", hook_relay.__file__], input="{not json", env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == "" and len(events(hub)) == 1


# ---------------------------------------------------------------- installer

CMD = "/usr/bin/python3 -I /x/hook_relay.py"


def test_install_hooks_diff_confirm_and_idempotent(tmp_path):
    s = tmp_path / "settings.json"
    seen = []
    diff = install_hooks(s, command=CMD, confirm=lambda d: seen.append(d) or True)
    assert seen == [diff] and "PermissionRequest" in diff and "PostToolUse" in diff and diff.startswith("---")
    data = json.loads(s.read_text())
    for ev in ("PermissionRequest", "PostToolUse"):
        (entry,) = data["hooks"][ev]
        assert entry["matcher"] == "*"
        assert entry["hooks"] == [{"type": "command", "command": CMD, "timeout": 5}]
    before = s.read_text()
    again = install_hooks(s, command=CMD, confirm=lambda d: pytest.fail("must not ask when nothing changes"))
    assert again == "" and s.read_text() == before


def test_install_hooks_preserves_existing_hooks(tmp_path):
    s = tmp_path / "settings.json"
    orig = {"model": "x", "permissions": {"allow": ["Bash(ls)"]},
            "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "fmt"}]}],
                      "Stop": [{"hooks": [{"type": "command", "command": "bye"}]}]}}
    s.write_text(json.dumps(orig, indent=2))
    os.chmod(s, 0o640)
    install_hooks(s, command=CMD, confirm=lambda d: True)
    data = json.loads(s.read_text())
    assert data["model"] == "x" and data["permissions"] == orig["permissions"]
    assert data["hooks"]["Stop"] == orig["hooks"]["Stop"]
    assert data["hooks"]["PostToolUse"][0] == orig["hooks"]["PostToolUse"][0]
    assert len(data["hooks"]["PostToolUse"]) == 2 and "PermissionRequest" in data["hooks"]
    assert (s.stat().st_mode & 0o777) == 0o640


def test_install_hooks_declined_writes_nothing(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text('{"a": 1}')
    install_hooks(s, command=CMD, confirm=lambda d: False)
    assert s.read_text() == '{"a": 1}' and [p.name for p in tmp_path.iterdir()] == ["settings.json"]
    missing = tmp_path / "new.json"
    install_hooks(missing, command=CMD, confirm=lambda d: False)
    assert not missing.exists()


def test_install_hooks_refuses_invalid_json(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text("{broken")
    with pytest.raises(ValueError):
        install_hooks(s, command=CMD, confirm=lambda d: True)
    assert s.read_text() == "{broken" and [p.name for p in tmp_path.iterdir()] == ["settings.json"]
    for bad in ('[]', '{"hooks": []}', '{"hooks": {"PostToolUse": {}}}'):
        s.write_text(bad)
        with pytest.raises(ValueError):
            install_hooks(s, command=CMD, confirm=lambda d: True)
        assert s.read_text() == bad


def test_install_hooks_backup_is_new_file(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text('{"a": 1}')
    install_hooks(s, command=CMD, confirm=lambda d: True)
    (bak,) = tmp_path.glob("settings.json.bak-*")
    assert bak.read_text() == '{"a": 1}'
    # a pre-existing backup of the same name is never overwritten
    s.write_text('{"b": 2}')
    stamp = bak.name.split(".bak-")[1]
    fixed = hook_relay._backup_path(s, stamp=stamp)
    assert fixed != bak
    bak.write_text("precious")
    install_hooks(s, command=CMD, confirm=lambda d: True)
    assert bak.read_text() == "precious"
    assert len(list(tmp_path.glob("settings.json.bak-*"))) == 2


# ---------------------------------------------------------------- redeem / install-hooks CLI

def redeem_args(srv, code, tmp_path, **kw):
    base = dict(action="redeem", url=f"http://127.0.0.1:{srv.port}", code=code, device_id="dev7",
                out=str(tmp_path / "out" / "assistant-worker.json"), force=False)
    base.update(kw)
    return Namespace(**base)


def test_redeem_writes_0600_and_prints_mcp_add_once(started, tmp_path, capsys):
    from herdr_engineering.cli import _cmd_cp_assistant_worker
    hub, srv = started()
    _, code = hub.registry.create_enrollment(tenant_id="default", user_id="u1", name="box", harness="claude")
    args = redeem_args(srv, code, tmp_path)
    assert _cmd_cp_assistant_worker(args) == 0
    out = capsys.readouterr()
    dest = Path(args.out)
    assert (dest.stat().st_mode & 0o777) == 0o600
    cfg = json.loads(dest.read_text())
    cred = cfg["credential"]
    assert cfg["url"] == f"http://127.0.0.1:{srv.port}" and cfg["device_id"] == "dev7"
    assert (out.out + out.err).count(cred) == 1
    assert f'claude mcp add --transport http herdr-assistant http://127.0.0.1:{srv.port}/assistant/mcp' in out.out
    assert f'--header "Authorization: Worker {cred}" --header "X-Herdr-Device: dev7"' in out.out
    assert "not be shown again" in out.out and "0600" in out.out
    assert RelayConfig.load(dest).credential == cred
    # re-running refuses to overwrite (the code is single-use anyway) and leaks nothing
    _, code2 = hub.registry.create_enrollment(tenant_id="default", user_id="u1", name="box2", harness="claude")
    assert _cmd_cp_assistant_worker(redeem_args(srv, code2, tmp_path)) == 1
    out2 = capsys.readouterr()
    assert cred not in out2.out + out2.err and json.loads(dest.read_text())["credential"] == cred
    assert _cmd_cp_assistant_worker(redeem_args(srv, code2, tmp_path, force=True)) == 0
    assert json.loads(dest.read_text())["credential"] != cred
    assert (dest.stat().st_mode & 0o777) == 0o600


def test_redeem_failure_is_one_line_no_secret(started, tmp_path, capsys):
    from herdr_engineering.cli import _cmd_cp_assistant_worker
    _, srv = started()
    args = redeem_args(srv, "SECRETCODE-123", tmp_path)
    assert _cmd_cp_assistant_worker(args) == 1
    out = capsys.readouterr()
    assert out.out == "" and len(out.err.strip().splitlines()) == 1 and "SECRETCODE" not in out.err
    assert not Path(args.out).exists()
    dead = redeem_args(srv, "SECRETCODE-123", tmp_path, url="http://127.0.0.1:1")
    assert _cmd_cp_assistant_worker(dead) == 1
    out = capsys.readouterr()
    assert len(out.err.strip().splitlines()) == 1 and "SECRETCODE" not in out.err


def test_cli_install_hooks_command_and_non_claude(tmp_path, capsys):
    from herdr_engineering.cli import _cmd_cp_assistant_worker
    s = tmp_path / "s.json"
    ns = Namespace(action="install-hooks", harness="claude", settings=str(s), yes=True)
    assert _cmd_cp_assistant_worker(ns) == 0
    cmd = json.loads(s.read_text())["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert cmd.startswith(sys.executable) and " -I " in cmd and cmd.endswith("hook_relay.py")
    capsys.readouterr()
    ns = Namespace(action="install-hooks", harness="pi", settings=str(tmp_path / "t.json"), yes=True)
    assert _cmd_cp_assistant_worker(ns) == 1
    assert "claude" in capsys.readouterr().err and not (tmp_path / "t.json").exists()
