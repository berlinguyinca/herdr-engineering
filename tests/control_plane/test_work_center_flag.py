"""WC-006: `work_center_v1` rollout gate. YAML-only (control_plane.work_center.enabled), strict True, default off."""
import json
import threading
import urllib.request

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def _health(**kw):
    svr = ControlPlaneServer(ControlPlaneRepo(), **kw)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{svr.port}/api/v1/health") as r:
            return json.loads(r.read())
    finally:
        svr.shutdown()


def test_capability_absent_by_default():
    assert "work_center_v1" not in _health()["capabilities"]


def test_capability_absent_for_anything_but_strict_true():
    for cfg in ({}, {"enabled": False}, {"enabled": "true"}, {"enabled": 1}, {"enabled": None}):
        assert "work_center_v1" not in _health(work_center_config=cfg)["capabilities"], cfg


def test_capability_present_when_enabled_and_additive():
    on = _health(work_center_config={"enabled": True})["capabilities"]
    off = _health()["capabilities"]
    assert "work_center_v1" in on
    assert [c for c in on if c != "work_center_v1"] == off          # nothing removed or reordered
    assert "unified_workspace_v1" not in on                          # independent of the workspace flag


def test_flag_is_independent_of_the_unified_workspace_flag():
    caps = _health(unified_workspace_config={"enabled": True})["capabilities"]
    assert "unified_workspace_v1" in caps and "work_center_v1" not in caps
