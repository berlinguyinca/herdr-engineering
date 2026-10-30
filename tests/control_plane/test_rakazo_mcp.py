# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Remote MCP servers as the assistant's connectors."""
import json

from tests.control_plane.test_rakazo_bridge import alice, make_server, srv  # noqa: F401
from tests.control_plane.test_rakazo_contract import validate

CONFIG = {"transport": "streamable_http", "name": "Docs Server", "description": "", "endpoint": "https://mcp.example.org/mcp",
          "headers": {"X-Team": "lab"}, "secret": "tok-123", "oauthClient": None}


def rpc(api, proc, inp, token):
    return api.rpc(proc, inp, token)[0]


def test_create_list_update_remove(srv, alice):
    svr, api = srv
    made = api.ok("mcp/servers/create", CONFIG, alice)
    validate("mcp/servers/create", made)
    assert (made["name"], made["transport"], made["headerKeys"], made["hasSecret"]) == (
        "Docs Server", "streamable_http", ["X-Team"], True)
    assert made["slug"] == "docs-server" and "tok-123" not in json.dumps(made)
    stored = svr.rakazo.svc.store.connectors[made["id"]]
    assert stored.kind == "mcp" and stored.auth["_allowed_origins"] == ["https://mcp.example.org"]
    listing = api.ok("mcp/servers/list", {}, alice)
    validate("mcp/servers/list", listing)
    assert [s["id"] for s in listing] == [made["id"]] and "tok-123" not in json.dumps(listing)
    out = api.ok("mcp/servers/update", {"id": made["id"], "config": {**CONFIG, "name": "Renamed", "secret": None}}, alice)
    validate("mcp/servers/update", out)
    assert out["name"] == "Renamed" and out["hasSecret"] is True                 # no new secret keeps the stored one
    rotated = api.ok("mcp/servers/update", {"id": made["id"], "secret": "tok-456"}, alice)
    assert svr.rakazo.svc.store.connectors[made["id"]].auth["secret"] == "tok-456" and "tok-456" not in json.dumps(rotated)
    validate("mcp/servers/test", api.ok("mcp/servers/test", {"id": made["id"]}, alice))
    validate("mcp/servers/remove", api.ok("mcp/servers/remove", {"id": made["id"]}, alice))
    assert api.ok("mcp/servers/list", {}, alice) == []
    assert made["id"] in svr.rakazo.svc.store.connectors                          # archived, never deleted
    assert rpc(api, "mcp/servers/test", {"id": made["id"]}, alice) == 404


def test_unsafe_servers_are_refused(srv, alice):
    _, api = srv
    for change in ({"transport": "stdio", "command": "rm", "args": []}, {"endpoint": "http://mcp.example.org"},
                   {"endpoint": "https://user:pw@mcp.example.org"}, {"name": ""}, {"headers": {"bad header": "x"}},
                   {"transport": "carrier-pigeon"}):
        assert rpc(api, "mcp/servers/create", {**CONFIG, **change}, alice) == 400, change
    assert api.ok("mcp/servers/list", {}, alice) == []
