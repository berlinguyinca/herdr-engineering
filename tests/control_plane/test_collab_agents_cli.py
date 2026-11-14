"""`herdr-eng collab agents list|renew`: the real CLI against a real CollabService behind the real operator listener."""
import json
import os
import stat

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


def connect(stack, name, harness="tui-smoke"):
    s, info = call(stack.op, "POST", f"{BASE}/agents/connect", {"harness_type": harness, "name": name},
                   host=f"127.0.0.1:{stack.op}")
    assert s == 201
    return info


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


def test_renew_without_a_destination_is_refused_before_any_request(stack, capsys):
    info = connect(stack, "nodest")
    assert run(stack, "renew", info["agent_id"]) == 2
    assert "--credential-file" in capsys.readouterr().err
    assert run(stack, "list", "--json") == 0
    assert agents_json(capsys)[0]["status"] == "active"
    assert call(stack.op, "POST", f"{BASE}/agents/{info['agent_id']}/renew", {},
                host=f"127.0.0.1:{stack.op}")[0] == 200           # the old credential was still live: nothing was renewed


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
