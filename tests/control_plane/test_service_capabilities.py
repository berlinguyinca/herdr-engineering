"""The optional-service capability flags that the dashboard Settings page lists.

Each flag must be true exactly when the feature's routes are usable, so the tests build real servers with and without the
feature and read /api/v1/health?probe=1. The artifact stores are real SyncArtifactAdapters around an inert store: the flags
only look at whether a service is configured, never at what the store holds.
"""
import json
import threading
import urllib.request

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.repo import ControlPlaneRepo

from .test_deployment_actions import World
from .test_review_gate_api import gate_server, monitor_starts  # noqa: F401 - fixtures

KEYS = {"opkey": "operator"}
SERVICE_FLAGS = {"share_v1", "members_v1", "collab_artifacts_v1", "review_gate_v1", "artifact_delete_v1",
                 "ci_actions_v1", "deployment_actions_v1"}


def caps_of(server, key="opkey"):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/health?probe=1",
                                     headers={"X-API-Key": key})
    with urllib.request.urlopen(request) as response:
        return set(json.loads(response.read())["capabilities"]) & SERVICE_FLAGS


def adapter():
    return SyncArtifactAdapter(lambda: object())


def served_caps(**kwargs):
    adapters = [v for v in kwargs.values() if isinstance(v, SyncArtifactAdapter)]
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys=KEYS, **kwargs)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        return caps_of(server)
    finally:
        server.shutdown()
        for a in adapters:
            a.close()


def test_defaults_advertise_no_service():
    assert served_caps() == set()


def test_share_needs_all_of_store_root_settings_and_artifacts(tmp_path):
    full = dict(artifact_store=adapter(), share_store=adapter(), shares_root=tmp_path, share_settings={})
    assert served_caps(**full) == {"share_v1"}
    assert served_caps(artifact_store=adapter(), share_store=adapter(), share_settings={}) == set()


def test_members_need_the_admin_on_top_of_sharing(tmp_path):
    assert served_caps(artifact_store=adapter(), share_store=adapter(), shares_root=tmp_path, share_settings={},
                       member_admin=adapter()) == {"share_v1", "members_v1"}


def test_collab_artifacts_needs_flag_and_grant_store():
    assert served_caps(artifact_store=adapter(), collab_grant_store=adapter()) == set()
    assert served_caps(artifact_store=adapter(), collab_grant_store=adapter(),
                       collab_artifacts_enabled=True) == {"collab_artifacts_v1"}


def test_artifact_delete_needs_an_owner_and_a_store():
    assert served_caps(artifact_store=adapter()) == set()
    assert served_caps(artifact_store=adapter(),
                       artifact_delete_owners=frozenset({"0123456789abcdef"})) == {"artifact_delete_v1"}


def test_ci_actions_follow_the_config_switch():
    assert served_caps(ci_actions=True) == {"ci_actions_v1"}


def test_review_gate_is_advertised_only_when_the_gate_runs(gate_server):  # noqa: F811
    assert caps_of(gate_server, "write") == {"review_gate_v1"}


def test_deployment_actions_need_the_flag_and_definitions(tmp_path):
    for name, actions in (("on", True), ("off", False)):
        (tmp_path / name).mkdir()
        world = World(tmp_path / name, actions=actions)
        try:
            assert ("deployment_actions_v1" in caps_of(world.svr)) is actions
        finally:
            world.svr.shutdown()
