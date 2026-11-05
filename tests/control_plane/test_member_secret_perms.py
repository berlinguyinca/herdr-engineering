# ruff: noqa: F811
"""The member-share secret URL crosses two uids: the worker (root) writes it, collab-public (HERDR_COLLAB_CERT_UID)
reads and deletes it.  The worker's file and directory therefore carry the collab-public group.

Real filesystem, real chown.  A different uid cannot be assumed here, so the group is a second group of the running
user (when it has one) and the deploy smoke script (deploy/control-plane/member-secret-smoke.sh) does the true
cross-uid read on the host."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering.control_plane.artifact_member_intents import (
    SECRETS_DIR,
    MemberRuntime,
    take_secret,
)
from herdr_engineering.control_plane.collab_bridge_files import link_new_beneath
from herdr_engineering.control_plane.members_config import parse_members
from tests.control_plane.registry_pg import registry_pg  # noqa: F401 - imported fixture
from tests.control_plane.test_member_share_flow import (
    ALICE,
    approve,
    published,
    share,
    srun,
    user,
)

ROOT = Path(__file__).resolve().parents[2]


def other_gid() -> int | None:
    return next((g for g in os.getgroups() if g != os.getgid()), None)


needs_group = pytest.mark.skipif(other_gid() is None, reason="the user has no second group")


@needs_group
def test_gid_sets_group_and_modes_on_a_new_and_a_stale_directory(tmp_path):
    gid = other_gid()
    assert link_new_beneath(tmp_path, [SECRETS_DIR], "a.json", b"{}", mode=0o640, create_dirs=True,
                            dir_mode=0o770, gid=gid)
    d = tmp_path / SECRETS_DIR
    assert (os.stat(d).st_gid, stat.S_IMODE(os.stat(d).st_mode)) == (gid, 0o770)
    f = d / "a.json"
    assert (os.stat(f).st_gid, stat.S_IMODE(os.stat(f).st_mode)) == (gid, 0o640)
    # a directory an older deploy created root-private is repaired, not left unreadable to collab-public
    stale = tmp_path / "stale"
    (stale / SECRETS_DIR).mkdir(parents=True)
    os.chmod(stale / SECRETS_DIR, 0o700)
    assert link_new_beneath(stale, [SECRETS_DIR], "b.json", b"{}", mode=0o640, create_dirs=True, dir_mode=0o770, gid=gid)
    assert (os.stat(stale / SECRETS_DIR).st_gid, stat.S_IMODE(os.stat(stale / SECRETS_DIR).st_mode)) == (gid, 0o770)


def test_without_gid_nothing_is_widened(tmp_path):
    assert link_new_beneath(tmp_path, [SECRETS_DIR], "a.json", b"{}", create_dirs=True, dir_mode=0o700)
    assert stat.S_IMODE(os.stat(tmp_path / SECRETS_DIR).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / SECRETS_DIR / "a.json").st_mode) == 0o600


def test_secrets_gid_is_a_config_key():
    base = {"enabled": True, "group": "staff", "session_secret": "a" * 40}
    assert parse_members(base, public_host="a.example.org")["secrets_gid"] is None
    assert parse_members({**base, "secrets_gid": 1234}, public_host="a.example.org")["secrets_gid"] == 1234
    for bad in (-1, True, "x", 1.5):
        with pytest.raises(ValueError, match="secrets_gid"):
            parse_members({**base, "secrets_gid": bad}, public_host="a.example.org")


@needs_group
def test_approve_hands_the_secret_to_the_collab_public_group(registry_pg, tmp_path):
    gid = other_gid()

    async def body(env, rt):
        aid, vid = await published(env)
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        ticket, done = await approve(env, user("alice"), aid, sid)
        assert done["result"] == "live"
        d = env.outbox / SECRETS_DIR
        f = d / f"{ticket}.json"
        assert (os.stat(d).st_gid, stat.S_IMODE(os.stat(d).st_mode)) == (gid, 0o770)
        assert (os.stat(f).st_gid, stat.S_IMODE(os.stat(f).st_mode)) == (gid, 0o640)
        assert take_secret(env.outbox, ticket, ALICE) is not None and not f.exists()

    srun(registry_pg, tmp_path, body, secret_gid=gid)


def test_runtime_defaults_keep_the_secret_private():
    assert MemberRuntime.__dataclass_fields__["secret_gid"].default is None


@pytest.mark.skipif(os.geteuid() != 0, reason="the cross-uid check needs root (run the deploy smoke script on the host)")
def test_smoke_script_crosses_two_uids(tmp_path):
    os.chmod(tmp_path, 0o755)
    out = subprocess.run(["bash", str(ROOT / "deploy/control-plane/member-secret-smoke.sh"), str(tmp_path), "65534"],
                         capture_output=True, text=True, env={**os.environ, "PYTHON": sys.executable,
                                                              "PYTHONPATH": str(ROOT)})
    assert out.returncode == 0, out.stdout + out.stderr


def test_smoke_script_is_shell_valid():
    assert subprocess.run(["bash", "-n", str(ROOT / "deploy/control-plane/member-secret-smoke.sh")]).returncode == 0
