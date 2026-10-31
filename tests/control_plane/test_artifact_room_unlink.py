"""Artifact-room links: link checks the room, unlink exists and needs edit, room grants follow, the repair removes the rest.

Real Postgres (registry_pg), a real CollabService and the real HTTP server; no stand-ins."""
import asyncio
import threading
from contextlib import contextmanager

import asyncpg

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifact_room_access import (
    RoomArtifactAccess,
    principal_for,
    revoke_room_grants_pg,
)
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.registry_pg import FakeBlobs, registry_pg  # noqa: F401 - imported fixture
from tests.control_plane.test_artifact_registry_ext_api import KEYS, call, principal
from tests.control_plane.test_collab import TOKENS, Clock, _digest


@contextmanager
def running(dsn):
    async def factory():
        return ArtifactStore(await asyncpg.connect(dsn), FakeBlobs())

    adapter = SyncArtifactAdapter(factory)
    acl = ArtifactAcl(on_change=lambda action, payload: adapter("record_acl_event", action, payload))
    collab = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in TOKENS.items()}), now=Clock())
    alice = collab.authenticate(f"Bearer {TOKENS['alice']}", None)
    pid = collab.create_project(alice, {"name": "p"})["project_id"]
    room = collab.create_room(alice, pid, {"name": "r"})["room_id"]
    server = ControlPlaneServer(ControlPlaneRepo(), artifact_store=adapter, artifact_acl=acl, rustfs=FakeBlobs(),
                                api_keys=KEYS, collab=collab)
    server.room_artifact_access = RoomArtifactAccess(acl, adapter, lambda room_id: {"bob": None}, now=lambda: 0.0)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        yield server, adapter, acl, room
    finally:
        server.shutdown()
        thread.join(3)
        adapter.close()


def new_artifact(adapter, acl, key="owner"):
    owner = principal(key)
    artifact_id = adapter("register_logical", original_filename="a.md", created_by=owner, mime_type="text/markdown")["artifact_id"]
    acl.grant(artifact_id, owner, {"read", "edit", "review", "share", "admin"})
    return artifact_id


def test_link_needs_a_room_that_exists_and_says_nothing_more_about_one_that_does_not(registry_pg):
    with running(registry_pg["dsn"]) as (server, adapter, acl, room):
        aid = new_artifact(adapter, acl)
        status, body = call(server, "POST", f"/api/v1/artifacts/{aid}/links", {"room_id": room})
        assert status == 201 and body["linked"] is True
        for unknown in ("room-does-not-exist", "x" * 40):
            status, body = call(server, "POST", f"/api/v1/artifacts/{aid}/links", {"room_id": unknown})
            assert (status, body) == (404, {"error": "not found"})
        links = call(server, "GET", f"/api/v1/artifacts/{aid}/links")[1]["links"]
        assert [link["room_id"] for link in links] == [room]


def test_unlink_removes_the_link_and_the_rooms_read_grants_and_is_idempotent(registry_pg):
    with running(registry_pg["dsn"]) as (server, adapter, acl, room):
        aid = new_artifact(adapter, acl)
        call(server, "POST", f"/api/v1/artifacts/{aid}/links", {"room_id": room})
        assert acl.can(aid, principal_for("bob"), "read")                      # the link synced the room's people
        status, body = call(server, "POST", f"/api/v1/artifacts/{aid}/links/remove", {"room_id": room})
        assert (status, body) == (200, {"artifact_id": aid, "unlinked": 1})
        assert call(server, "GET", f"/api/v1/artifacts/{aid}/links")[1]["links"] == []
        assert not acl.can(aid, principal_for("bob"), "read")
        assert call(server, "POST", f"/api/v1/artifacts/{aid}/links/remove", {"room_id": room})[1]["unlinked"] == 0


def test_unlink_needs_edit_and_answers_a_stranger_like_a_missing_artifact(registry_pg):
    with running(registry_pg["dsn"]) as (server, adapter, acl, room):
        aid = new_artifact(adapter, acl)
        call(server, "POST", f"/api/v1/artifacts/{aid}/links", {"room_id": room})
        acl.grant(aid, principal("other"), {"read"})                           # can read, cannot edit
        assert call(server, "POST", f"/api/v1/artifacts/{aid}/links/remove", {"room_id": room}, key="other") == (404, {"error": "not found"})
        assert call(server, "POST", f"/api/v1/artifacts/{aid}/links/remove", {"room_id": room}, key="agent")[0] == 403   # agent role is read-only
        assert call(server, "POST", "/api/v1/artifacts/missing/links/remove", {"room_id": room}, key="other") == (404, {"error": "not found"})
        assert len(call(server, "GET", f"/api/v1/artifacts/{aid}/links")[1]["links"]) == 1


def test_unlink_rejects_bodies_that_name_anything_but_a_room(registry_pg):
    with running(registry_pg["dsn"]) as (server, adapter, acl, room):
        aid = new_artifact(adapter, acl)
        for body in ({}, {"room_id": ""}, {"room_id": room, "project_id": "p"}, {"project_id": "p"}):
            assert call(server, "POST", f"/api/v1/artifacts/{aid}/links/remove", body)[0] == 400


def test_cli_unlink_removes_the_link_through_the_real_route(registry_pg, monkeypatch, capsys):
    from herdr_engineering.cli import main
    monkeypatch.setenv("HERDR_API_KEY", "owner")
    with running(registry_pg["dsn"]) as (server, adapter, acl, room):
        aid = new_artifact(adapter, acl)
        base = f"http://127.0.0.1:{server.port}"
        assert main(["artifacts", "link", aid, "--room", room, "--control-plane", base]) == 0
        capsys.readouterr()
        assert main(["artifacts", "unlink", aid, "--room", room, "--control-plane", base, "--json"]) == 0
        assert '"unlinked": 1' in capsys.readouterr().out.replace('":1', '": 1')
        assert call(server, "GET", f"/api/v1/artifacts/{aid}/links")[1]["links"] == []
        assert main(["artifacts", "unlink", "missing", "--room", room, "--control-plane", base]) == 1


def test_cli_revoke_room_grants_is_a_dry_run_until_apply(registry_pg, tmp_path, capsys):
    import yaml

    from herdr_engineering.cli import main
    config = tmp_path / "fleet.yaml"
    config.write_text(yaml.safe_dump({"version": 1, "hosts": {}, "sections": {"control_plane": {
        "roles": ["control-plane"], "values": {"pg_dsn": registry_pg["dsn"]}}}}))
    config.chmod(0o600)
    with running(registry_pg["dsn"]) as (server, adapter, acl, room):
        aid = new_artifact(adapter, acl)
        acl.grant(aid, principal_for("zed"), {"read"}, granted_by="room:" + room)
        assert main(["control-plane", "revoke-room-grants", "--fleet-config", str(config)]) == 0
        assert '"applied": false' in capsys.readouterr().out
        assert main(["control-plane", "revoke-room-grants", "--fleet-config", str(config), "--apply"]) == 0
        assert '"applied": true' in capsys.readouterr().out
        assert main(["control-plane", "revoke-room-grants", "--fleet-config", str(config), "--apply"]) == 0
        assert '"found": 0' in capsys.readouterr().out


def test_repair_removes_every_room_grant_from_the_database_dry_run_first_and_is_idempotent(registry_pg):
    with running(registry_pg["dsn"]) as (server, adapter, acl, room):
        aid = new_artifact(adapter, acl)
        bob, carol = principal_for("bob"), principal_for("carol")

        async def run(apply):
            conn = await asyncpg.connect(registry_pg["dsn"])
            try:
                return await revoke_room_grants_pg(conn, apply=apply)
            finally:
                await conn.close()

        async def rows():
            conn = await asyncpg.connect(registry_pg["dsn"])
            try:
                return sorted(r["principal_id"] for r in await conn.fetch(
                    "SELECT principal_id FROM artifact_acl WHERE artifact_id=$1", aid))
            finally:
                await conn.close()
        before = asyncio.run(run(False))["found"]                              # the database is shared by this module's tests
        acl.grant(aid, bob, {"read"}, granted_by="room:" + room)
        acl.grant(aid, carol, {"read", "edit"}, granted_by="control-plane")
        assert asyncio.run(run(False)) == {"found": before + 1, "removed": 0, "applied": False}
        assert asyncio.run(rows()) == sorted([principal("owner"), bob, carol])
        assert asyncio.run(run(True)) == {"found": before + 1, "removed": before + 1, "applied": True}
        assert asyncio.run(rows()) == sorted([principal("owner"), carol])
        assert asyncio.run(run(True)) == {"found": 0, "removed": 0, "applied": True}
