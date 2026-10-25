# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""The room's properties over the real HTTP listeners: PUT and PATCH set them like POST, with the same defences."""
from __future__ import annotations

from tests.control_plane.test_collab_public import stack  # noqa: F401

MiB = 1024 * 1024



def test_put_and_patch_reach_the_settings_over_http_on_every_listener(stack):
    """The real listeners (main with the TLS-proxy header, public): GET reads, POST/PUT/PATCH set; a contributor is
    refused; PUT/PATCH elsewhere stay unknown; a cross-site PUT and a non-JSON PATCH are refused like a POST."""
    from tests.control_plane.test_collab import BASE, TOKENS  # noqa: PLC0415
    from tests.control_plane.test_collab_public import H, main_api  # noqa: PLC0415
    svc = stack.svc
    alice = svc.authenticate(f"Bearer {TOKENS['alice']}", None)
    bob = svc.authenticate(f"Bearer {TOKENS['bob']}", None)
    pid = svc.create_project(alice, {"name": "p"})["project_id"]
    room = svc.create_room(alice, pid, {"name": "r"})["room_id"]
    svc.add_member(alice, room, {"user_id": bob.id, "role": "contributor"})
    url = f"{BASE}/rooms/{room}/settings"
    for api in (main_api(stack), stack.pub):
        call = (lambda *a, _api=api, **k: _api.call(*a, headers={**H, **k.pop("headers", {})}, **k))
        for method, value in (("PUT", 300 * MiB), ("PATCH", None), ("POST", 200 * MiB)):
            st, body = call(method, url, {"files_room_bytes": value}, user="alice")
            assert st == 200 and body["files_room_bytes"] == value, (method, st, body)
        st, body = call("GET", url, user="bob")
        assert st == 200 and body["files_room_bytes_effective"] == 200 * MiB and body["can_edit"] is False
        assert call("PATCH", url, {"files_room_bytes": 1}, user="bob")[0] == 403
        assert call("PUT", url, {"files_room_bytes": 1}, user="alice", headers={"Origin": "https://evil.example"})[0] == 403
        assert call("PATCH", url, {"files_room_bytes": 1}, user="alice", headers={"Content-Type": "text/plain"})[0] == 415
        assert call("PUT", "/api/v1/missions", {"x": 1}, user="alice")[0] in (404, 405)
    assert svc.get_room_settings(bob, room)["files_room_bytes"] == 200 * MiB

