# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""``join.sh --fabric``: the real script run by bash against the real fabric listener. No token, no worker, no credential."""
from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.control_plane.test_collab_fabric_auth import fabric_agent, fw, lan_ip  # noqa: F401
from tests.control_plane.test_collab_fabric_autojoin import aw, lw, set_trust  # noqa: F401
from tests.control_plane.test_collab_fabric_trust import localapi  # noqa: F401
from tests.control_plane.test_collab_tasks import code, svc  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packages/collab-scripts/join.sh"
pytestmark = pytest.mark.skipif(not (shutil.which("curl") and shutil.which("bash") and shutil.which("python3")),
                                reason="needs curl, bash and python3")


def run(lw, tmp_path, *opts, token=None, url=True):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    script = tmp_path / "join.sh"
    script.write_text(SCRIPT.read_text().replace("__HERDR_BASE__", f"http://{lw.lan}:{lw.public_port}"))
    script.chmod(stat.S_IRWXU)
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(home)}
    if token:
        env["HERDR_JOIN_TOKEN"] = token
    extra = ["--fabric-url", f"http://{lw.lan}:{lw.fabric_port}"] if url else []
    p = subprocess.run(["bash", str(script), "--fabric", *extra, *opts], env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def ident(tmp_path, name="bender-claude"):
    return tmp_path / "home" / "herdr-agents" / f"{name}.json"


def test_fabric_join_writes_only_agent_id_name_and_machine_at_0600(lw, tmp_path):
    rc, out, err = run(lw, tmp_path, "--name", "bender-claude")
    assert rc == 0, (out, err)
    data = json.loads(ident(tmp_path).read_text())
    assert set(data) == {"agent_id", "name", "machine"} and data["machine"] == "bender"
    assert data["agent_id"] in lw.s._agents
    assert stat.S_IMODE(ident(tmp_path).stat().st_mode) == 0o600
    assert not (tmp_path / "home/herdr-agents/worker.sh").exists()           # no worker: there is no credential to run one


def test_fabric_join_is_idempotent_and_needs_no_token(lw, tmp_path):
    run(lw, tmp_path, "--name", "bender-claude")
    first = ident(tmp_path).read_text()
    rc, _, err = run(lw, tmp_path, "--name", "bender-claude")
    assert rc == 0, err
    assert ident(tmp_path).read_text() == first


def test_fabric_join_ignores_a_join_token(lw, tmp_path):
    rc, out, err = run(lw, tmp_path, "--name", "bender-claude", token="hcj_secretsecretsecret")
    assert rc == 0, err
    assert "hcj_secret" not in ident(tmp_path).read_text() + out + err


def test_fabric_join_needs_a_clean_name(lw, tmp_path):
    for bad in ("../x", "a/b", "A", "x" * 65):
        rc, _, _ = run(lw, tmp_path, "--name", bad)
        assert rc != 0
    rc, _, err = run(lw, tmp_path)
    assert rc != 0 and "--name" in err
    assert not (tmp_path / "home" / "herdr-agents").exists() or not list((tmp_path / "home" / "herdr-agents").glob("*.json"))


def test_a_refusal_is_reported_and_writes_nothing(lw, tmp_path):
    set_trust(lw, auto_join=False)
    rc, _, err = run(lw, tmp_path, "--name", "bender-claude")
    assert rc != 0 and "refused" in err
    assert not ident(tmp_path).exists()


def test_the_token_path_is_unchanged_without_fabric(lw, tmp_path):
    script = tmp_path / "join.sh"
    script.write_text(SCRIPT.read_text().replace("__HERDR_BASE__", f"http://{lw.lan}:{lw.public_port}"))
    p = subprocess.run(["bash", str(script)], env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, capture_output=True, text=True, timeout=30)
    assert p.returncode == 1 and "HERDR_JOIN_TOKEN" in p.stderr
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_fabric_needs_the_listener_url_and_says_so_before_any_request(lw, tmp_path):
    rc, out, err = run(lw, tmp_path, "--name", "bender-claude", url=False)
    assert rc == 2 and not out
    assert "--fabric-url" in err and "fabric listener" in err
    assert not (tmp_path / "home" / "herdr-agents").exists() or not list((tmp_path / "home" / "herdr-agents").glob("*.json"))


# ONE accept/reject table for the three places a name is judged: the server, `rooms join-fabric` and join.sh
ACCEPT = ["a", "a.b", "a_b-c", "0x", "bender-claude", "a" * 64]
REJECT = ["", "A", "a b", "-a", ".a", "_a", "a/b", "../x", "a\n", "\na", "a" * 65, "é", "a;b", "a$b", "a\tb", "a\x00b"]


def test_the_client_uses_the_servers_name_rule():
    from herdr_engineering import rooms_client
    from herdr_engineering.control_plane.collab_fabric_trust import JOIN_NAME

    assert rooms_client._JOIN_NAME is JOIN_NAME                              # one rule, not a copy
    assert all(JOIN_NAME.fullmatch(n) for n in ACCEPT)
    assert not any(JOIN_NAME.fullmatch(n) for n in REJECT)


def test_the_server_judges_names_by_the_table(aw):
    from tests.control_plane.test_collab_fabric_autojoin import join

    aw.s.fabric_join_per_hour = aw.s.fabric_join_max_agents = 1000
    for n in ACCEPT:
        assert join(aw, name=n)[0] == 201, repr(n)
    for n in REJECT:
        assert join(aw, name=n)[0] == 400, repr(n)


def test_join_sh_judges_names_by_the_table(tmp_path):
    script = tmp_path / "join.sh"
    script.write_text(SCRIPT.read_text().replace("__HERDR_BASE__", "http://127.0.0.1:9"))
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)}
    for locale in ("C", "en_US.UTF-8"):
        for n in ACCEPT + REJECT:
            if n == "" or "\x00" in n:        # argv cannot carry NUL; an empty name is "--name needs a name" territory
                continue
            p = subprocess.run(["bash", str(script), "--fabric", "--name", n, "--fabric-url", "http://127.0.0.1:9"],
                               env=env | {"LC_ALL": locale}, capture_output=True, text=True, timeout=30)
            # accepted: it reaches curl, which cannot connect (rc 1). Rejected: refused up front (rc 2).
            assert p.returncode == (1 if n in ACCEPT else 2), (locale, n, p.stderr)
