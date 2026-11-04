import asyncio
import hashlib

from herdr_engineering.durable_public_gateway import DurablePublicGateway
from herdr_engineering.public_gateway import GatewayResponse


class _Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class _S3:
    def get_object(self, *, Bucket, Key):
        return {"Body": _Body(b"durable-public")}


class _Conn:
    async def fetchrow(self, sql, *args):
        return {"state": "active", "expires_at": None,
                "output_manifest": [{"name": "index.html", "storage_key": "k",
                                     "sha256": hashlib.sha256(b"durable-public").hexdigest(),
                                     "media_type": "text/html"}]}


def test_durable_gateway_reads_only_approved_pinned_output():
    calls = []

    async def async_call(method, *args):
        calls.append((method, args))
        if method == "deliver":
            return await DurablePublicGateway(_Conn(), _S3(), bucket="private").deliver(*args)
        raise AssertionError(method)

    def call(method, *args):
        return asyncio.run(async_call(method, *args))

    gateway = DurablePublicGateway.from_sync(call)
    response = gateway.request("opaque-token", method="GET", path="/index.html")
    assert isinstance(response, GatewayResponse)
    assert response.status == 200
    assert response.body == b"durable-public"
    assert calls and calls[0][0] == "deliver"

