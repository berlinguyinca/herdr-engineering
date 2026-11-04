"""Wire the shared memory journal/index into the existing threaded control plane.

Configuration, credentials and domain membership are operator-owned. No new
HTTP service, database, vector implementation, or public exposure is created.

Operator cp.memory accepts inline backend_url/domains/credentials plus optional
operator_domains/allow_private_http/credential_owner_uid, OR registry_file.
Only these registry fields are copied from inline config. Domain credentials
select exactly one backend_token or backend_token_file; never request input.
"""

import asyncio
import copy
import json
import os
import secrets
import stat
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from .artifact_adapter import SyncArtifactAdapter


def _registry(path, owner_uid):
    fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != owner_uid:
            raise ValueError(
                "memory registry must be a private file owned by its configured operator"
            )
        raw = source.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("memory registry is too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("memory registry must be an object")
    return data


def _index_credentials(domain):
    sources = [key for key in ("backend_token", "backend_token_file") if key in domain]
    if len(sources) != 1:
        raise ValueError("memory domain requires exactly one backend credential source")
    source = sources[0]
    value = domain[source]
    if not isinstance(value, str) or not value:
        raise ValueError("invalid memory backend credential source")
    return {"token" if source == "backend_token" else "token_file": value}


def _load_registry(config, owner_uid):
    fields = {
        "backend_url",
        "domains",
        "credentials",
        "operator_domains",
        "allow_private_http",
        "credential_owner_uid",
    }
    inline = fields.intersection(config)
    if "registry_file" in config:
        if inline:
            raise ValueError("inline memory registry and registry_file are mutually exclusive")
        registry = _registry(config["registry_file"], owner_uid)
    else:
        registry = copy.deepcopy({key: config[key] for key in inline})
    if (
        not isinstance(registry.get("backend_url"), str)
        or not registry["backend_url"]
        or not isinstance(registry.get("domains"), dict)
        or not isinstance(registry.get("credentials"), dict)
    ):
        raise ValueError("memory registry requires backend_url, domains and credentials")
    for domain in registry["domains"].values():
        if not isinstance(domain, dict):
            raise ValueError("invalid memory domain")
        _index_credentials(domain)
    return registry


class _Index:
    def __init__(self, delegate, can_write):
        self.delegate, self.can_write = delegate, can_write

    def retain(self, row):
        if not self.can_write():
            raise RuntimeError("memory index lease unavailable")
        return self.delegate.retain(row)

    def forget(self, memory_id):
        if not self.can_write():
            raise RuntimeError("memory index lease unavailable")
        return self.delegate.forget(memory_id)

    def recall(self, *args, **kwargs):
        return self.delegate.recall(*args, **kwargs)


class _Boundary:
    @classmethod
    async def create(cls, dsn, registry):
        import asyncpg
        from herdr_memory.transport import Transport

        from .assistant_hindsight import AssistantHindsight
        from .assistant_memory_evidence import verify_git_quote
        from .assistant_memory_gateway import MemoryGateway
        from .assistant_memory_store import AssistantMemoryStore

        self = cls()
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, command_timeout=10)
        self.lease = None
        self.owns_lease = False
        self._lease_lock = asyncio.Lock()
        try:
            async with self.pool.acquire() as conn:
                await conn.fetch("SELECT domain_type,domain_id FROM pa_memory LIMIT 0")

            @asynccontextmanager
            async def stores(domain, actor):
                async with self.pool.acquire() as conn:
                    yield AssistantMemoryStore(
                        conn,
                        domain["tenant_id"],
                        actor.id,
                        "harness:" + actor.harness,
                        domain_type=domain["domain_type"],
                        domain_id=domain["domain_id"],
                    )

            def indexes(domain):
                transport = Transport(
                    url=registry["backend_url"],
                    **_index_credentials(domain),
                    timeout=5,
                    token_owner_uid=registry.get("credential_owner_uid"),
                    allow_private_http=registry.get("allow_private_http", False),
                )
                index = AssistantHindsight(
                    (domain["tenant_id"], domain["domain_type"], domain["domain_id"]),
                    transport,
                    bank_ids=domain["bank_ids"],
                )
                return _Index(index, self.can_index)

            credentials = copy.deepcopy(registry["credentials"])
            worker_key = secrets.token_urlsafe(32)
            credentials[worker_key] = {
                "harness": "herdr-indexer",
                "domains": list(registry["domains"]),
                "read_only": False,
            }
            self.gateway = MemoryGateway(
                registry["domains"],
                credentials,
                stores,
                indexes,
                evidence_checker=verify_git_quote,
                operator_domains=registry.get("operator_domains", []),
            )
            self.worker = self.gateway.authenticate("Bearer " + worker_key)
            self.state = {
                "enabled": True,
                "domain_count": len(registry["domains"]),
                "last_delivery_pass": None,
                "indexer": False,
            }
            await self._lease()
            return self
        except BaseException:
            if self.lease is not None:
                await self.pool.release(self.lease)
            await self.pool.close()
            raise

    def can_index(self):
        return bool(self.owns_lease and self.lease is not None and not self.lease.is_closed())

    async def _lease(self):
        async with self._lease_lock:
            await self._lease_locked()

    async def _lease_locked(self):
        if self.lease is not None and self.lease.is_closed():
            await self.pool.release(self.lease)
            self.lease = None
            self.owns_lease = False
        if self.lease is None:
            self.lease = await self.pool.acquire(timeout=5)
        if not self.owns_lease:
            self.owns_lease = bool(
                await self.lease.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended('herdr-memory-indexer-v1',0))"
                )
            )
        self.state["indexer"] = self.can_index()

    async def authorize(self, authorization):
        return self.gateway.authenticate(authorization)

    async def authorize_operator(self, principal):
        return self.gateway.authorize_operator(principal)

    async def operator_domains(self, actor):
        return await self.gateway.operator_domains(actor)

    async def health(self):
        return dict(self.state)

    async def native(self, method, path, body, actor):
        await self._lease()
        return await self.gateway.native(method, path, body, actor)

    async def drain_all(self):
        await self._lease()
        if not self.can_index():
            return
        started = time.monotonic()
        for alias in self.gateway.domains:
            await self.gateway.drain(self.worker, alias, limit=1)
            if time.monotonic() - started >= 10:
                break
        self.state["last_delivery_pass"] = time.time()

    async def close(self):
        if self.lease is not None:
            await self.pool.release(self.lease)
            self.lease = None
        await self.pool.close()


class MemoryRuntime:
    def __init__(self, adapter):
        self.adapter = adapter
        self._stop = threading.Event()
        self._thread = None
        self._closed = False

    def __call__(self, method, *args):
        return self.adapter(method, *args)

    def health(self):
        try:
            return self.adapter("health")
        except Exception:
            return {"enabled": True, "status": "unavailable"}

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._drain, daemon=True, name="herdr-memory-delivery"
        )
        self._thread.start()

    def _drain(self):
        while not self._stop.wait(3):
            try:
                self.adapter("drain_all")
            except Exception:
                # Durable work stays pending. Neither content nor backend errors
                # belong in logs or a browser response.
                pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=16)
        try:
            self.adapter("close")
        finally:
            self.adapter.close()


def build_memory_runtime(dsn, config):
    config = config or {}
    if not config.get("enabled", False):
        return None
    if config.get("enabled") is not True or not dsn:
        raise ValueError("enabled memory requires durable PostgreSQL")
    owner = config.get("registry_owner_uid", os.getuid())
    if type(owner) is not int or owner < 0:
        raise ValueError("invalid memory registry owner")
    registry = _load_registry(config, owner)
    adapter = SyncArtifactAdapter(lambda: _Boundary.create(dsn, registry))
    return MemoryRuntime(adapter)
