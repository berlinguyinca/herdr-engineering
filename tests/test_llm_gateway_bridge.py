import hashlib
import json
import stat

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.llm_gateway_bridge import (render_principals, run, sync_once,
                                                  write_if_changed)


class RepoControl:
    def __init__(self, repo):
        self.repo = repo

    def config(self):
        return self.repo.llm_config()


class Down:
    def config(self):
        raise OSError("control plane down")


def test_a_minted_key_becomes_a_prehashed_principal_and_plaintext_never_appears(tmp_path):
    repo = ControlPlaneRepo()
    minted = repo.create_project_key("proj", "ci")
    path = tmp_path / "principals.json"
    assert sync_once(RepoControl(repo), path) is True
    body = path.read_text()
    digest = hashlib.sha256(minted["key"].encode()).hexdigest()
    assert json.loads(body) == {f"herdr:proj:{minted['key_id']}": f"sha256:{digest}"}
    assert minted["key"] not in body
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_revoked_keys_leave_the_file_and_an_unchanged_set_is_not_rewritten(tmp_path):
    repo = ControlPlaneRepo()
    k = repo.create_project_key("proj")
    path = tmp_path / "p.json"
    assert sync_once(RepoControl(repo), path) is True
    assert sync_once(RepoControl(repo), path) is False
    repo.revoke_project_key(k["key_id"])
    assert sync_once(RepoControl(repo), path) is True
    assert json.loads(path.read_text()) == {}


def test_unreachable_control_plane_keeps_the_last_file(tmp_path):
    path = tmp_path / "p.json"
    write_if_changed(path, {"herdr:a:k": "sha256:" + "0" * 64})
    before = path.read_text()
    assert sync_once(Down(), path) is None
    assert path.read_text() == before


def test_malformed_keys_are_skipped_not_forwarded():
    good = "a" * 64
    cfg = {"keys": [{"key_id": "k1", "project": "ok", "key_hash": good},
                    {"key_id": "k2", "project": "bad name", "key_hash": good},
                    {"key_id": "k3", "project": "ok", "key_hash": "NOTHEX"}]}
    assert render_principals(cfg) == {"herdr:ok:k1": "sha256:" + good}


def test_the_loop_keeps_the_file_current_without_touching_anything_else(tmp_path):
    repo = ControlPlaneRepo()
    first = repo.create_project_key("proj")
    path = tmp_path / "p.json"
    steps = []

    def sleep(_):
        steps.append(json.loads(path.read_text()))
        if len(steps) == 1:
            repo.revoke_project_key(first["key_id"])
            repo.create_project_key("proj")

    run(RepoControl(repo), path, interval=0, sleep=sleep, stop=lambda: len(steps) >= 2)
    assert len(steps[0]) == 1 and len(steps[1]) == 1
    assert list(steps[0]) != list(steps[1]), "revoked key replaced by the new one"
