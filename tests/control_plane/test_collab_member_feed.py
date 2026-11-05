# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""Plan 5, Task 5: the member directory feed (``member-directory.json``), the per-agent ``groups`` / ``harness_type``
fields of ``agents.json``, ``CollabService.member_check`` and the MCP large-body admission for member chunks.

A real ``CollabService`` in approval mode (the groups come from real ``verify_claims`` claims at sign-in), a real
``CollabArtifactBridge`` writing to a temp outbox, a real ``ControlPlaneServer`` on loopback sockets."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane import collab_mcp
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_member_intents import MemberRuntime, load_directory
from herdr_engineering.control_plane.artifact_members import member_for_actor
from herdr_engineering.control_plane.collab_artifacts import AGENTS_FEED, CollabArtifactBridge
from herdr_engineering.control_plane.collab_bridge_files import (
    MCP_UPLOAD_MAX_BODY,
    read_json_beneath,
)
from herdr_engineering.control_plane.collab_intents import AGENTS_FEED_MAX_BYTES
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab_approval import IAT, TOKENS, call, who
from tests.control_plane.test_collab_approval import s as approval_svc  # noqa: F401
from tests.control_plane.test_collab_external import enroll
from tests.control_plane.test_collab_mcp_artifacts import KIB, padded, post_msg, raw_post

DIRECTORY = "member-directory.json"
GROUP = "local.dev"          # op and lab carry it in TOKENS; bob, carol, dave have no group; mal has "Users"


def directory(outbox: Path):
    return read_json_beneath(outbox, [DIRECTORY], max_bytes=16 * 1024 * 1024)


def feed(outbox: Path):
    return read_json_beneath(outbox, [AGENTS_FEED], max_bytes=AGENTS_FEED_MAX_BYTES)


class Net:
    """approval-mode service with member_group set, a bridge on a temp outbox, and signed-in people."""

    def __init__(self, s, tmp_path):
        self.s = s
        s.member_group = GROUP
        self.outbox = tmp_path / "out"
        self.outbox.mkdir()
        self.bridge = CollabArtifactBridge(s, tmp_path / "mirror", self.outbox, clock=s._now, feed_interval=3600.0)
        s.artifact_bridge = self.bridge
        self.people = {}

    def person(self, name):
        pr = who(self.s, name)
        if pr.status != "approved":              # outsiders are approved people too, just not in the member group
            self.approve(name)
            pr = who(self.s, name)
        self.people[name] = pr
        return pr

    def approve(self, name):
        call(self.s, "POST", ["admin", "users", name, "approve"], "adm")

    def agent(self, name, **body):
        info = self.s.connect_agent(self.people[name], {"harness_type": "claude-code", **body})
        pr = self.s.authenticate(f"Agent {info['credential']}", info["device_id"])
        return pr, info


@pytest.fixture
def net(approval_svc, tmp_path):
    n = Net(approval_svc, tmp_path)
    yield n
    n.bridge.stop()


# ------------------------------------------------------------------------------ the member directory
def test_the_directory_lists_group_users_with_email_and_marks_others_removed(net):
    for name in ("op", "lab", "mal", "bob"):
        net.person(name)
    net.bridge.write_agents_feed()
    doc = directory(net.outbox)
    assert doc["group"] == GROUP and doc["updated"] == net.s._now()
    assert set(doc["users"]) == {"op", "lab", "mal", "adm"}       # bob has no groups claim: no evidence, not listed
    assert doc["users"]["mal"] == {"email": None, "display_name": "mal", "status": "removed", "groups": ["Users"],
                                  "removed_since": net.s._now()}
    assert doc["users"]["op"] == {"email": "op@example.org", "display_name": "op", "status": "approved",
                                  "groups": [GROUP]}


def test_a_disabled_member_is_listed_as_disabled(net):
    net.person("op")
    net.person("lab")
    net.bridge.write_agents_feed()
    call(net.s, "POST", ["admin", "users", "lab", "disable"], None, operator="op")
    doc = directory(net.outbox)
    assert doc["users"]["lab"]["status"] == "disabled" and doc["users"]["op"]["status"] == "approved"


def test_a_user_without_an_email_has_a_null_email_and_the_id_as_display_name(net):
    net.s.authenticate("Bearer t-lab", None)        # signed in, identity page not visited yet
    net.bridge.write_agents_feed()
    assert directory(net.outbox)["users"]["lab"] == {"email": None, "display_name": "lab", "status": "approved",
                                                     "groups": [GROUP]}


def test_losing_the_group_marks_the_user_removed_on_the_next_sign_in(net):
    net.person("op")
    net.bridge.write_agents_feed()
    assert directory(net.outbox)["users"]["op"]["status"] == "approved"
    TOKENS["t-op"] = ("op", ["Users"])
    try:
        net.bridge._wake.clear()
        net.s.authenticate("Bearer t-op", None)
        assert net.bridge._wake.is_set()                           # the change asks the feed thread for an early rewrite
        net.bridge.write_agents_feed()
        assert directory(net.outbox)["users"]["op"]["status"] == "removed"
    finally:
        TOKENS["t-op"] = ("op", ["local.dev"])


def test_expired_group_knowledge_is_published_as_unknown_never_removed(net):
    net.person("op")
    net.bridge.write_agents_feed()
    net.s._now.t += net.s.member_groups_ttl + 1
    net.bridge.write_agents_feed()
    assert directory(net.outbox)["users"]["op"]["status"] == "unknown"
    net.s._now.t += net.s.MEMBER_FORGET_AFTER
    net.bridge.write_agents_feed()
    assert "op" not in directory(net.outbox)["users"]             # forgotten after a month: absence, still no evidence


def test_a_token_without_a_groups_claim_refreshes_nothing(net):
    net.person("op")
    TOKENS["t-op"] = ("op", [])
    try:
        net.s._now.t += net.s.member_groups_ttl - 10
        net.s.authenticate("Bearer t-op", None)
        net.s._now.t += 20
        net.bridge.write_agents_feed()
        assert directory(net.outbox)["users"]["op"]["status"] == "unknown"   # not "removed", not refreshed
    finally:
        TOKENS["t-op"] = ("op", ["local.dev"])


def make_service(tmp_path, groups_path):
    from herdr_engineering.control_plane.collab import ApprovalPolicy, CollabService, FileEventLog
    from tests.control_plane.test_collab_approval import Clock, OpenAuth
    return CollabService(OpenAuth(), sink=FileEventLog(str(tmp_path / "log2")), now=Clock(),
                         approval=ApprovalPolicy(auto_groups=["local.dev"], approver_groups=["Administrators"]),
                         member_group=GROUP, member_groups_path=groups_path)


def test_claims_survive_a_restart_in_a_private_file(net, tmp_path):
    import os
    import stat
    path = tmp_path / "state" / "member-groups.json"
    first = make_service(tmp_path, path)
    first.authenticate("Bearer t-op", None)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = make_service(tmp_path, path)                           # restart: new instance, empty memory
    assert again.member_directory()["users"]["op"]["status"] in ("approved", "unknown")
    assert again.member_directory()["users"]["op"]["groups"] == [GROUP]


def test_a_restart_with_no_saved_claims_publishes_nobody_rather_than_everyone_removed(tmp_path):
    fresh = make_service(tmp_path, tmp_path / "gone" / "member-groups.json")
    assert fresh.member_directory() == {"group": GROUP, "users": {}}


def test_a_damaged_claims_file_is_ignored(tmp_path):
    bad = tmp_path / "member-groups.json"
    bad.write_text("{not json")
    assert make_service(tmp_path, bad).member_directory()["users"] == {}


def test_the_directory_file_is_private_and_written_before_the_agents_feed(net):
    import os
    import stat
    net.person("op")
    net.bridge.write_agents_feed()
    assert stat.S_IMODE(os.stat(net.outbox / DIRECTORY).st_mode) == 0o600
    assert os.stat(net.outbox / DIRECTORY).st_mtime_ns <= os.stat(net.outbox / AGENTS_FEED).st_mtime_ns


def test_an_unknown_member_is_refused_with_directory_unknown(net):
    pr = net.person("op")
    assert net.s.member_check(pr).ok
    net.s._now.t += net.s.member_groups_ttl + 1                     # the claims age out; no new sign-in
    verdict = net.s.member_check(pr)
    assert not verdict.ok and verdict.reason == "directory_unknown"
    lone = member_for_actor({"kind": "user", "user_id": "op"}, agents={}, group=GROUP,
                            directory={"op": {"status": "unknown", "groups": [GROUP]}})
    assert lone.reason == "directory_unknown"


def test_no_directory_without_a_configured_group(approval_svc, tmp_path):
    s = approval_svc
    (tmp_path / "out").mkdir()
    br = CollabArtifactBridge(s, tmp_path / "mirror", tmp_path / "out", clock=s._now, feed_interval=3600.0)
    s.artifact_bridge = br
    who(s, "op")
    br.write_agents_feed()
    assert not (tmp_path / "out" / DIRECTORY).exists()
    assert s.member_directory() is None


def test_a_wrong_type_group_is_refused_at_construction():
    from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
    for bad in (5, "", ["a"]):
        with pytest.raises(ValueError):
            CollabService(StaticTokenAuthenticator({}), member_group=bad)


# ------------------------------------------------------------------------------ agents.json
def test_the_agent_feed_carries_a_real_boolean_external_groups_and_harness_type(net):
    net.person("op")
    net.person("mal")
    a, _ = net.agent("op", harness_type="pi")
    m, _ = net.agent("mal")
    pid = net.s.create_project(net.people["op"], {"name": "p"})["project_id"]
    room = net.s.create_room(net.people["op"], pid, {"name": "r"})["room_id"]
    ext_pr, _, _ = enroll(SimpleNamespace(s=net.s, alice=net.people["op"], room=room))
    net.bridge.write_agents_feed()
    agents = feed(net.outbox)["agents"]
    assert agents[a.id]["external"] is False and agents[a.id]["groups"] == [GROUP]
    assert agents[a.id]["harness_type"] == "pi"
    assert agents[m.id]["groups"] == ["Users"] and agents[m.id]["harness_type"] == "claude-code"
    assert agents[ext_pr.id]["external"] is True
    for row in agents.values():
        assert isinstance(row["external"], bool) and isinstance(row["groups"], list)


def test_an_owner_never_seen_signing_in_has_empty_groups(net):
    net.person("op")
    a, _ = net.agent("op")
    net.s._user_groups.clear()
    net.bridge.write_agents_feed()
    assert feed(net.outbox)["agents"][a.id]["groups"] == []


def test_the_worker_reads_both_feeds_and_admits_the_members_agent_only(net):
    for name in ("op", "mal"):
        net.person(name)
    a, _ = net.agent("op")
    m, _ = net.agent("mal")
    net.bridge.write_agents_feed()
    rt = MemberRuntime(outbox=net.outbox, mirror_root=net.outbox, group=GROUP, enabled=True, defaults={},
                       shares_root=None, clock=net.s._now)
    loaded = load_directory(rt)
    agents = feed(net.outbox)["agents"]
    ok = member_for_actor({"kind": "agent", "agent_id": a.id}, agents=agents, group=GROUP, directory=loaded)
    assert ok.ok and ok.principal == "member:op"
    no = member_for_actor({"kind": "agent", "agent_id": m.id}, agents=agents, group=GROUP, directory=loaded)
    assert not no.ok and no.reason == "not_in_group"


# ------------------------------------------------------------------------------ member_check
def test_member_check_admits_the_internal_agent_of_an_approved_group_member(net):
    net.person("op")
    a, _ = net.agent("op")
    chk = net.s.member_check(a)
    assert chk.ok and chk.principal == "member:op"
    assert net.s.member_check(net.people["op"]).principal == "member:op"        # a user principal too


def test_member_check_refuses_outsiders_externals_and_the_disabled(net):
    for name in ("op", "lab", "mal", "bob"):
        net.person(name)
    outsider, _ = net.agent("mal")
    assert not net.s.member_check(outsider).ok
    pid = net.s.create_project(net.people["op"], {"name": "p"})["project_id"]
    room = net.s.create_room(net.people["op"], pid, {"name": "r"})["room_id"]
    ext_pr, _, _ = enroll(SimpleNamespace(s=net.s, alice=net.people["op"], room=room))
    chk = net.s.member_check(ext_pr)
    assert not chk.ok and chk.reason == "external_agent"
    a, _ = net.agent("lab")
    assert net.s.member_check(a).ok
    call(net.s, "POST", ["admin", "users", "lab", "disable"], None, operator="op")
    chk = net.s.member_check(a)
    assert not chk.ok and chk.reason == "user_disabled"


def test_member_check_is_off_without_a_configured_group(approval_svc):
    s = approval_svc
    pr = who(s, "op")
    info = s.connect_agent(pr, {})
    agent = s.authenticate(f"Agent {info['credential']}", info["device_id"])
    chk = s.member_check(agent)
    assert not chk.ok and chk.reason == "members_disabled"


def test_collab_side_imports_never_pull_in_asyncpg():
    code = ("import sys; import herdr_engineering.control_plane.collab as c; "
            "import herdr_engineering.control_plane.artifact_members as m; "
            "sys.exit(1 if 'asyncpg' in sys.modules else 0)")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# ------------------------------------------------------------------------------ the MCP large-body gate
@pytest.fixture
def edge(net, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=net.s, public_port=0,
                             web_root=web, collab_require_https=True)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    yield srv
    srv.shutdown()
    t.join(timeout=5)
    collab_mcp.reset_large_body_admissions()


def cred(info):
    return {"credential": info["credential"], "device_id": info["device_id"]}


@pytest.fixture
def agents(net, edge):
    for name in ("op", "mal", "lab"):
        net.person(name)
    out = SimpleNamespace(member=net.agent("op")[1], outsider=net.agent("mal")[1], other=net.agent("lab")[1])
    net.bridge.write_agents_feed()
    return out


def test_a_member_agent_without_a_write_grant_may_send_a_700kib_upload_chunk(edge, agents):
    status, _, body = post_msg(edge, cred(agents.member), padded("artifact_upload_chunk", 700 * KIB))
    assert status == 200 and "error" not in json.loads(body)      # past the gate (the tool itself is plan 5 task 6)


def test_a_member_agent_may_not_send_other_big_tools(edge, agents):
    for tool in ("artifact_stage_chunk", "room_post", "artifact_commit"):
        status, _, body = post_msg(edge, cred(agents.member), padded(tool, 700 * KIB))
        assert status == 200 and json.loads(body)["error"]["code"] == -32602, tool
        assert json.loads(body)["error"]["message"] == "request too large for this tool"


def test_an_internal_non_member_agent_gets_413_without_the_body_being_read(edge, agents):
    status, hdrs, _ = raw_post(edge, cred(agents.outsider), 700 * KIB, send_body=False)
    assert status == 413 and hdrs["connection"] == "close"


def test_a_member_who_is_disabled_loses_the_allowance(edge, net, agents):
    call(net.s, "POST", ["admin", "users", "lab", "disable"], None, operator="op")
    status, hdrs, _ = raw_post(edge, cred(agents.other), 700 * KIB, send_body=False)
    assert status == 413 and hdrs["connection"] == "close"


def test_an_external_agent_of_a_member_gets_no_allowance(edge, net, agents):
    pid = net.s.create_project(net.people["op"], {"name": "p"})["project_id"]
    room = net.s.create_room(net.people["op"], pid, {"name": "r"})["room_id"]
    _, out, _ = enroll(SimpleNamespace(s=net.s, alice=net.people["op"], room=room))
    status, hdrs, _ = raw_post(edge, cred(out), 700 * KIB, send_body=False)
    assert status == 413 and hdrs["connection"] == "close"


def test_over_the_upload_cap_is_413_even_for_a_member(edge, agents):
    status, hdrs, _ = raw_post(edge, cred(agents.member), MCP_UPLOAD_MAX_BODY + 1, send_body=False)
    assert status == 413 and hdrs["connection"] == "close"


def test_unauthenticated_and_bad_credentials_are_413_and_counted_once(edge, net, agents):
    status, hdrs, _ = raw_post(edge, None, 700 * KIB, send_body=False)
    assert status == 413 and hdrs["connection"] == "close"
    before = net.s.stats["auth_failures"]
    status, _, _ = raw_post(edge, {"credential": "nope", "device_id": "d"}, 700 * KIB, send_body=False)
    assert status == 413 and net.s.stats["auth_failures"] == before + 1


def test_a_member_batch_cannot_smuggle_a_forbidden_tool(edge, agents):
    batch = [padded("artifact_upload_chunk", 600 * KIB), padded("room_post", 20 * KIB, rid=2)]
    status, _, body = post_msg(edge, cred(agents.member), batch)
    assert status == 200 and json.loads(body)["error"]["code"] == -32602


def test_the_member_admission_is_in_the_baseline_so_a_reset_keeps_it(edge, net, agents):
    collab_mcp.reset_large_body_admissions()
    pr = net.s.authenticate(f"Agent {agents.member['credential']}", agents.member["device_id"])
    assert collab_mcp.admitted_tools(net.s, pr) == {"artifact_upload_chunk"}


# ------------------------------------------------------------------------------ config and worker wiring
def collab(**members):
    if members.get("enabled") is True:        # an enabled section needs the signing secret and the member host (slice form)
        members = {"session_secret": "k" * 40, "public_host": "artifacts.example.org", **members}
    return {"collab": {"members": members}}


@pytest.mark.parametrize("cp", [{}, {"collab": {}}, {"collab": None}, collab(), collab(enabled=False),
                                collab(enabled=False, group=5)])
def test_members_are_off_unless_enabled_is_exactly_true(cp):
    from herdr_engineering import cli
    assert cli._members_settings(cp) is None


def test_enabled_members_read_group_ttl_and_optional_limits():
    from herdr_engineering import cli
    got = cli._members_settings(collab(enabled=True, group="staff", groups_ttl=60, max_member_bytes=10,
                                       max_artifact_bytes=None))
    assert {k: v for k, v in got.items() if k not in ("session_secret", "public_host", "limits")} == {
        "group": "staff", "groups_ttl": 60.0, "removed_grace": 3600.0,
        "defaults": {"max_artifact_bytes": None, "max_member_bytes": 10, "max_artifacts": None},
        "share_limits": {}, "share_creates_per_hour": 20}
    shares = cli._members_settings(collab(enabled=True, group="staff", share_creates_per_hour=5,
                                          share_limits={"max_share_bytes": 9, "max_total_bytes": None}))
    assert shares["share_creates_per_hour"] == 5
    assert shares["share_limits"] == {"max_share_bytes": 9, "max_total_bytes": None}
    for bad in ({"share_creates_per_hour": 0}, {"share_creates_per_hour": True}, {"share_limits": []},
                {"share_limits": {"max_share_bytes": 0}}, {"share_limits": {"max_share_bytes": "9"}},
                {"share_limits": {"nope": 1}}):
        with pytest.raises(SystemExit):
            cli._members_settings(collab(enabled=True, group="staff", **bad))
    plain = cli._members_settings(collab(enabled=True, group="staff"))
    assert cli._members_settings(collab(enabled=True, group="staff", removed_grace=0))["removed_grace"] == 0.0
    with pytest.raises(SystemExit):
        cli._members_settings(collab(enabled=True, group="staff", removed_grace=-1))
    assert plain["groups_ttl"] == 24 * 3600.0 and plain["defaults"] == {
        "max_artifact_bytes": None, "max_member_bytes": None, "max_artifacts": None}


@pytest.mark.parametrize("bad", [{"group": ""}, {}, {"group": 3}, {"group": "g", "groups_ttl": 0},
                                 {"group": "g", "groups_ttl": True}, {"group": "g", "max_artifacts": 0},
                                 {"group": "g", "max_member_bytes": True}, {"group": "g", "max_member_bytes": "9"},
                                 {"group": "g", "nonsense": 1}, {"group": "g", "enabled": "yes"}])
def test_an_invalid_members_section_stops_startup_with_the_key_named(bad):
    from herdr_engineering import cli
    with pytest.raises(SystemExit) as err:
        cli._members_settings(collab(**{"enabled": True, **bad}))
    assert "collab.members" in str(err.value)


def test_collab_config_accepts_the_members_key_and_validates_it():
    import argparse

    from herdr_engineering import cli
    args = argparse.Namespace(**{dest: ([] if kind is list else None if kind is not bool else False)
                                 for dest, kind in cli.COLLAB_CONFIG_KEYS.values()})
    cli._apply_collab_config(args, collab(enabled=True, group="staff"))
    with pytest.raises(SystemExit, match="collab.members"):
        cli._apply_collab_config(args, collab(enabled=True, group=""))


def test_the_worker_runtime_points_at_the_bridge_dirs_and_the_share_dir(tmp_path):
    from herdr_engineering import cli
    cp = {"collab_artifacts": {"enabled": True, "mirror_dir": str(tmp_path / "m"), "outbox_dir": str(tmp_path / "o"),
                               "spool_dir": str(tmp_path / "s")},
          "share": {"enabled": True, "shares_dir": str(tmp_path / "shares"), "public_url": "https://s.example"},
          **collab(enabled=True, group="staff", max_member_bytes=7, share_limits={"max_share_bytes": 5})}
    settings = cli._collab_artifacts_settings(cp)
    rt = cli._members_runtime(cp, settings)
    assert (rt.group, rt.enabled, rt.outbox, rt.mirror_root) == ("staff", True, tmp_path / "o", tmp_path / "m")
    assert rt.shares_root == tmp_path / "shares" and rt.defaults["max_member_bytes"] == 7
    assert rt.share_settings["member_share_limits"] == {"max_share_bytes": 5}
    assert rt.share_settings["default_public_days"] > 0
    no_share = cli._members_runtime({**cp, "share": {"enabled": False}}, settings)
    assert no_share.shares_root is None and no_share.share_settings is None
    assert cli._members_runtime({"collab_artifacts": cp["collab_artifacts"]}, settings) is None


def test_the_built_loop_gets_the_members_runtime_and_its_fourth_task(tmp_path):
    import asyncio

    from herdr_engineering import cli
    from tests.control_plane.registry_pg import FakeBlobs
    cp = {"collab_artifacts": {"enabled": True, "mirror_dir": str(tmp_path / "m"), "outbox_dir": str(tmp_path / "o"),
                               "spool_dir": str(tmp_path / "s")}, **collab(enabled=True, group="staff")}
    (tmp_path / "o").mkdir()
    settings = cli._collab_artifacts_settings(cp)
    rt = cli._members_runtime(cp, settings)

    async def connect():
        raise AssertionError("not connected in this test")

    async def go():
        blobs = FakeBlobs()
        loop = cli._build_collab_bridge_loop(settings, mirror=cli._build_collab_mirror(settings, rustfs=blobs),
                                             rustfs=blobs, connect=connect, lineage=None, members=rt)
        assert loop.applier.extras["members"] is rt and loop.members_publisher is not None
        loop.mirror.close()
    asyncio.run(go())


def test_the_service_is_built_with_the_member_group(approval_svc):
    from herdr_engineering import cli
    kwargs = cli._collab_member_kwargs(collab(enabled=True, group="staff", groups_ttl=90))
    assert kwargs == {"member_group": "staff", "member_groups_ttl": 90.0}
    withdir = cli._collab_member_kwargs(collab(enabled=True, group="staff", groups_ttl=90), state_dir=Path("/var/c"))
    assert withdir["member_groups_path"] == Path("/var/c/member-groups.json")
    assert cli._collab_member_kwargs({}) == {}


# ------------------------------------------------------------------------------ the members task wakes early
def test_the_members_task_runs_again_at_once_when_notified(tmp_path):
    import asyncio

    from herdr_engineering import cli
    from tests.control_plane.registry_pg import FakeBlobs

    cp = {"collab_artifacts": {"enabled": True, "mirror_dir": str(tmp_path / "m"), "outbox_dir": str(tmp_path / "o"),
                               "spool_dir": str(tmp_path / "s")}, **collab(enabled=True, group="staff")}
    (tmp_path / "o").mkdir()
    settings = cli._collab_artifacts_settings(cp)
    runs = []

    async def publisher(conn):                      # the loop's injected seam, here only counting its calls
        runs.append(conn)

    async def connect():
        raise AssertionError("not connected in this test")

    async def go():
        blobs = FakeBlobs()
        loop = cli._build_collab_bridge_loop(settings, mirror=cli._build_collab_mirror(settings, rustfs=blobs),
                                             rustfs=blobs, connect=connect, lineage=None,
                                             members=cli._members_runtime(cp, settings))
        loop.members_publisher, loop.members_every = publisher, 3600.0
        stop = asyncio.Event()
        task = asyncio.create_task(loop._members("conn", stop, None))
        await asyncio.sleep(0.2)
        assert runs == ["conn"]                     # the first pass only; the next one is an hour away
        loop.notify_members()
        await asyncio.sleep(0.2)
        assert runs == ["conn", "conn"]
        stop.set()
        await asyncio.wait_for(task, 5)
        loop.mirror.close()
    asyncio.run(go())


# ------------------------------------------------------------------------------ ordering, grace, hostile state
@pytest.fixture
def newer_group_claims():
    """Restore the shared fake token table after a test that edits it."""
    saved = dict(TOKENS), dict(IAT)
    yield
    TOKENS.clear()
    TOKENS.update(saved[0])
    IAT.clear()
    IAT.update(saved[1])


def status_of(net, uid="op"):
    return directory(net.outbox)["users"][uid]


def test_an_older_token_cannot_turn_an_approved_member_into_removed(net, newer_group_claims):
    IAT["t-op"] = 200.0
    net.person("op")                                               # approved, claim issued at 200
    TOKENS["t-old"] = ("op", ["Users"])                            # a still-valid token issued BEFORE the group add
    IAT["t-old"] = 100.0
    net.s.authenticate("Bearer t-old", None)
    net.bridge.write_agents_feed()
    assert status_of(net)["status"] == "approved" and "removed_since" not in status_of(net)
    IAT["t-new"], TOKENS["t-new"] = 300.0, ("op", ["Users"])        # a NEWER token without the group is real
    net.s.authenticate("Bearer t-new", None)
    net.bridge.write_agents_feed()
    assert status_of(net)["status"] == "removed"


def test_a_token_without_iat_can_never_record_a_removal(net, newer_group_claims):
    net.person("op")
    seen = []

    class NoIat:
        def verify_claims(self, token):
            return {"user": "op", "exp": 4e9, "groups": ["Users"]}

        def exists(self, uid):
            return True
    real, net.s.auth = net.s.auth, NoIat()
    try:
        net.s.authenticate("Bearer anything", None)
    finally:
        net.s.auth = real
    net.bridge.write_agents_feed()
    assert status_of(net)["status"] == "approved", seen


def test_removed_since_is_sticky_and_a_later_approved_claim_clears_it(net, newer_group_claims):
    net.person("op")
    TOKENS["t-op"], IAT["t-op"] = ("op", ["Users"]), 10.0
    net.s.authenticate("Bearer t-op", None)
    first = net.s._now()
    net.s._now.t += 100
    IAT["t-op"] = 11.0
    net.s.authenticate("Bearer t-op", None)                         # still removed: the clock is NOT restarted
    net.bridge.write_agents_feed()
    assert status_of(net)["removed_since"] == first
    TOKENS["t-op"], IAT["t-op"] = ("op", [GROUP]), 12.0
    net.s.authenticate("Bearer t-op", None)
    net.bridge.write_agents_feed()
    assert status_of(net)["status"] == "approved" and "removed_since" not in status_of(net)
    TOKENS["t-op"], IAT["t-op"] = ("op", ["Users"]), 13.0
    net.s._now.t += 5
    net.s.authenticate("Bearer t-op", None)
    net.bridge.write_agents_feed()
    assert status_of(net)["removed_since"] == net.s._now()          # a fresh removal starts a fresh grace period


def test_ordering_and_removed_since_survive_a_restart(net, tmp_path, newer_group_claims):
    path = tmp_path / "state" / "member-groups.json"
    first = make_service(tmp_path, path)
    IAT["t-op"] = 50.0
    first.authenticate("Bearer t-op", None)
    TOKENS["t-old"], IAT["t-old"] = ("op", ["Users"]), 5.0
    again = make_service(tmp_path, path)
    again.authenticate("Bearer t-old", None)                        # older than the persisted iat: still ignored
    assert again._user_groups["op"].groups == (GROUP,) and again._user_groups["op"].iat == 50.0
    TOKENS["t-new"], IAT["t-new"] = ("op", ["Users"]), 60.0
    again.authenticate("Bearer t-new", None)
    third = make_service(tmp_path, path)
    assert third._user_groups["op"].removed_since == again._user_groups["op"].removed_since is not None


def test_a_capped_claim_without_the_group_is_no_evidence(net):
    net.person("op")
    net.s._note_groups("op", ["a", "b"], 5.0, True)                 # the verifier cut the list at 64
    assert net.s._user_groups["op"].groups == (GROUP,)
    net.s._note_groups("op", ["a", GROUP], 6.0, True)               # a capped claim that HAS the group is fine
    assert GROUP in net.s._user_groups["op"].groups


def test_cognito_flags_a_group_list_past_the_cap():
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from herdr_engineering.control_plane.collab_cognito import CognitoAuthenticator
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = CognitoAuthenticator(pool_id="us-west-2_abc", client_ids=["cid"], required_group="Users", open_signup=True)
    auth._key_for = lambda kid: key.public_key()
    base = {"iss": auth.issuer, "sub": "u", "username": "u", "client_id": "cid", "token_use": "access",
            "exp": 4e9, "iat": 1700000000}

    def verify(groups):
        return auth.verify_claims(jwt.encode({**base, "cognito:groups": groups}, key, algorithm="RS256",
                                             headers={"kid": "k"}))
    few, many = verify(["a"]), verify([f"g{i}" for i in range(70)])
    assert few["groups_capped"] is False and few["iat"] == 1700000000.0
    assert many["groups_capped"] is True and len(many["groups"]) == 64


@pytest.mark.parametrize("payload", [b"[" * 200_000 + b"]" * 200_000, b'{"users": ' * 50_000 + b"1" + b"}" * 50_000,
                                     b'{"users": {"op": {"at": "x"}}}', b'{"users": {"op": 3}}', b"\xff\xfe",
                                     b'{"users": {"op": {"at": 1e999, "groups": ["a"]}}}'])
def test_hostile_claims_files_mean_no_knowledge_and_never_crash_startup(tmp_path, payload):
    bad = tmp_path / "member-groups.json"
    bad.write_bytes(payload)
    assert make_service(tmp_path, bad).member_directory() == {"group": GROUP, "users": {}}
