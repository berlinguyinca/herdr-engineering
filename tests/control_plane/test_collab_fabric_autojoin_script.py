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
