"""Delivery-only public artifact gateway.

This module has no control-plane or S3 credentials. It resolves only an
approved publication's pinned resource/version through an injected resolver.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class GatewayResponse:
    status: int
    body: bytes = b""
    headers: dict[str, str] | None = None


class PublicArtifactGateway:
    def __init__(self, controller, *, resolver: Callable[[str, str], tuple[bytes, str] | None]):
        self.controller = controller
        self.resolver = resolver

    def request(self, token: str, *, method: str, path: str,
                guest_id: str | None = None) -> GatewayResponse:
        if method not in {"GET", "HEAD"}:
            return GatewayResponse(405, headers={"Allow": "GET, HEAD"})
        if not token or not path.startswith("/") or ".." in path:
            return GatewayResponse(404)
        publication = self.controller.lookup_token(token)
        if publication is None:
            return GatewayResponse(404)
        if publication["state"] == "expired" or publication["expires_at"] <= self.controller.now():
            self.controller.is_deliverable(publication["publication_id"])
            return GatewayResponse(410)
        if (publication["audience"] == "authenticated_guest" and guest_id is None):
            return GatewayResponse(401)
        if not self.controller.is_deliverable(publication["publication_id"], guest_id=guest_id):
            if publication["audience"] == "authenticated_guest":
                return GatewayResponse(403)
            return GatewayResponse(404)
        resolved = self.resolver(publication["resource_id"], publication["pinned_version_id"])
        if resolved is None:
            return GatewayResponse(404)
        body, media_type = resolved
        headers = {
            "Content-Type": media_type,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        return GatewayResponse(200, b"" if method == "HEAD" else body, headers)

