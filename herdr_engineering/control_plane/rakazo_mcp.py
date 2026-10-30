"""Remote MCP servers for the Rakazo mobile bridge, as the assistant's own connectors (kind ``mcp``).

A server the person adds in the app becomes an assistant connector: https only, with the operator's origin-allowlist rule
applied to its own origin, credentials kept in the connector's ``auth`` (never returned, never logged). The assistant's existing
MCP connector adapter is what reaches it. ``stdio`` servers run a command on this machine, so they are refused (the bridge also
reports ``mcpStdioEnabled: false``). "Remove" archives the connector (it is disabled and hidden; the record stays).
"""
from __future__ import annotations

import re
import socket
from typing import Any
from urllib.parse import urlsplit

from . import rakazo_events as ev
from .assistant import AssistantError

_TRANSPORTS = ("streamable_http", "sse")
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,120}\Z")
MAX_SERVERS = 50



_resolve = socket.getaddrinfo          # tests replace this: no DNS in a unit test


def _public_host(host: str) -> bool:
    """True only when every address the name resolves to is a public one (no loopback, private, link-local, reserved or
    multicast). The check runs when a server is saved; the operator's network policy still governs the request itself."""
    import ipaddress
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addrs = {ipaddress.ip_address(i[4][0]) for i in _resolve(host, 443, proto=socket.IPPROTO_TCP)}
        except (OSError, ValueError):
            return False
    return bool(addrs) and all(a.is_global for a in addrs)


class Mcp:
    def __init__(self, bridge, error_type):
        self.b = bridge
        self.Error = error_type

    def procs(self) -> dict[str, Any]:
        return {"mcp/servers/list": self.list, "mcp/servers/create": self.create, "mcp/servers/update": self.update,
                "mcp/servers/remove": self.remove, "mcp/servers/test": self.test}

    def _servers(self, ctx):
        return [c for c in self.b.svc.list_connectors(tenant_id=ctx.tenant, user_id=ctx.user)
                if c.kind == "mcp" and c.enabled]

    def _one(self, ctx, connector_id):
        for c in self._servers(ctx):
            if c.connector_id == connector_id:
                return c
        raise self.Error(404, "NOT_FOUND", "Not found.")

    def _view(self, ctx, c, message: str | None = None) -> dict[str, Any]:
        auth = c.auth or {}
        slug = re.sub(r"[^a-z0-9]+", "-", c.name.lower()).strip("-") or "server"
        return {"id": c.connector_id, "spaceId": ctx.space, "installId": None, "slug": slug, "name": c.name,
                "description": "", "transport": auth.get("_transport", "streamable_http"), "endpoint": c.base_url,
                "command": None, "hasArgs": False, "envKeys": [], "fileKeys": [],
                "headerKeys": sorted(auth.get("headers") or {}), "hasSecret": bool(auth.get("secret")),
                "oauthStatus": "none", "oauthClientId": None,
                "health": {"status": "unknown", "message": message, "toolCount": None, "checkedAt": None},
                "enabled": c.enabled, "revision": 1, "createdAt": ev.iso(c.created_at), "updatedAt": ev.iso(c.updated_at)}

    def _config(self, config: Any) -> tuple[str, str, str, dict[str, str], str | None]:
        if not isinstance(config, dict):
            raise self.Error(400, "BAD_REQUEST", "Provide the server configuration.")
        transport = config.get("transport")
        if transport == "stdio":
            raise self.Error(400, "BAD_REQUEST", "Local (stdio) MCP servers run commands on the server, so they are not "
                             "available here. Use a remote (https) MCP server.")
        if transport not in _TRANSPORTS:
            raise self.Error(400, "BAD_REQUEST", "transport must be streamable_http or sse.")
        name, endpoint = config.get("name"), config.get("endpoint")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise self.Error(400, "BAD_REQUEST", "A server needs a name of up to 80 characters.")
        parts = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if parts is None or parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise self.Error(400, "BAD_REQUEST", "The endpoint must be an https address without a login.")
        if not _public_host(parts.hostname):
            raise self.Error(400, "BAD_REQUEST", "The endpoint must be a public address, not one on a private network.")
        headers = config.get("headers") or {}
        if (not isinstance(headers, dict) or len(headers) > 16
                or not all(isinstance(k, str) and _HEADER.match(k) and isinstance(v, str) and len(v) <= 4096
                           for k, v in headers.items())):
            raise self.Error(400, "BAD_REQUEST", "headers must be up to 16 name/value pairs.")
        secret = config.get("secret")
        if secret is not None and (not isinstance(secret, str) or len(secret) > 16384):
            raise self.Error(400, "BAD_REQUEST", "The secret is too long.")
        return name.strip(), endpoint, transport, dict(headers), secret or None

    def list(self, ctx, inp):
        return [self._view(ctx, c) for c in sorted(self._servers(ctx), key=lambda c: c.name.casefold())]

    def create(self, ctx, inp):
        name, endpoint, transport, headers, secret = self._config(inp)
        if len(self._servers(ctx)) >= MAX_SERVERS:
            raise self.Error(400, "BAD_REQUEST", f"This server allows at most {MAX_SERVERS} MCP servers.")
        origin = f"https://{urlsplit(endpoint).netloc}"
        try:
            c = self.b.svc.register_connector(tenant_id=ctx.tenant, user_id=ctx.user, name=name, kind="mcp",
                                              base_url=endpoint,
                                              auth={"_allowed_origins": [origin], "_transport": transport,
                                                    "headers": headers, "secret": secret})
        except AssistantError as exc:
            raise self.Error(400, "BAD_REQUEST", str(exc)) from None
        return self._view(ctx, c)

    def update(self, ctx, inp):
        c = self._one(ctx, inp.get("id"))
        store = self.b.svc.store
        if "secret" in inp and "config" not in inp:
            if not isinstance(inp["secret"], str) or not 0 < len(inp["secret"]) <= 16384:
                raise self.Error(400, "BAD_REQUEST", "The secret must be 1 to 16384 characters.")
            c.auth = {**c.auth, "secret": inp["secret"]}
        else:
            name, endpoint, transport, headers, secret = self._config(inp.get("config"))
            origin = f"https://{urlsplit(endpoint).netloc}"
            c.name, c.base_url = name, endpoint
            c.auth = {**c.auth, "_allowed_origins": [origin], "_transport": transport, "headers": headers,
                      "secret": secret if secret is not None else c.auth.get("secret")}
        store.put_connector(c)
        return self._view(ctx, c)

    def remove(self, ctx, inp):
        c = self._one(ctx, inp.get("id"))
        self.b.svc.archive_connector(c.connector_id, tenant_id=ctx.tenant, user_id=ctx.user)
        return {"ok": True}

    def test(self, ctx, inp):
        c = self._one(ctx, inp.get("id"))
        return self._view(ctx, c, "Not probed: the assistant checks the server when it uses it.")
