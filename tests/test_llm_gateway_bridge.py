import hashlib
import json
import stat
import sys
import time

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.llm_gateway_bridge import (render_principals, supervise, sync_once,
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


def test_supervisor_restarts_the_gateway_once_per_debounce_and_only_on_change(tmp_path):
    repo = ControlPlaneRepo()
    repo.create_project_key("proj")
    starts = tmp_path / "starts"
    cmd = [sys.executable, "-c",
           f"open({str(starts)!r},'a').write('x');import time;time.sleep(60)"]
    now = [0.0]
    ticks = [0]

    def sleep(_):
        time.sleep(0.4)  # let the child process get as far as writing its start mark
        ticks[0] += 1
        now[0] += 10
        if ticks[0] == 2:
            repo.create_project_key("proj")   # a change inside the debounce window

    supervise(RepoControl(repo), tmp_path / "p.json", cmd, interval=1, debounce=30,
              sleep=sleep, clock=lambda: now[0], stop=lambda: ticks[0] >= 6)
    # started once, restarted once after the 30 s window, never per key
    assert starts.read_text() == "xx"
