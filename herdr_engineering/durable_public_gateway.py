"""Durable delivery-only resolver for the isolated public gateway process."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

from .public_gateway import GatewayResponse


class DurablePublicGateway:
    def __init__(self, conn, s3, *, bucket: str, sync_call: Callable | None = None):
        self.conn = conn
        self.s3 = s3
        self.bucket = bucket
        self._sync_call = sync_call

    @classmethod
    def from_sync(cls, sync_call):
        gateway = cls(None, None, bucket="", sync_call=sync_call)
        return gateway

    def request(self, token: str, *, method: str, path: str,
                guest_id: str | None = None) -> GatewayResponse:
        if method not in {"GET", "HEAD"}:
            return GatewayResponse(405, headers={"Allow": "GET, HEAD"})
        if not token or not path.startswith("/") or ".." in path:
            return GatewayResponse(404)
        if self._sync_call is not None:
            result = self._sync_call("deliver", token, path)
        else:
            raise RuntimeError("durable gateway requires a sync database adapter")
        if result is None:
            return GatewayResponse(404)
        if result.get("status") == 410:
            return GatewayResponse(410)
        body = result["body"]
        headers = {"Content-Type": result["media_type"],
                   "Cache-Control": "no-store",
                   "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
                   "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer",
                   "Content-Length": str(len(body))}
        return GatewayResponse(200, b"" if method == "HEAD" else body, headers)

    async def deliver(self, token: str, path: str):
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = await self.conn.fetchrow(
            "SELECT p.desired_state AS state, p.expires_at, v.output_manifest "
            "FROM publications p JOIN artifact_versions v ON v.version_id=p.pinned_version_id "
            "WHERE p.token_hash=$1 AND p.resource_type='artifact'", digest)
        if row is None or row["state"] != "active":
            return None
        expiry = row.get("expires_at")
        if expiry is not None:
            if isinstance(expiry, datetime):
                expiry_value = expiry.timestamp()
            else:
                expiry_value = float(expiry)
            if expiry_value <= datetime.now(UTC).timestamp():
                return {"status": 410}
        manifest = row["output_manifest"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        requested = path.lstrip("/") or "index.html"
        output = next((item for item in manifest if item.get("name") == requested), None)
        if output is None:
            return None
        if hasattr(self.s3, "get"):
            body = self.s3.get(output["storage_key"], output["sha256"])
        else:
            body = self.s3.get_object(Bucket=self.bucket, Key=output["storage_key"])["Body"].read()
        return {"body": body, "media_type": output.get("media_type", "application/octet-stream")}

