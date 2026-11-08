"""A real public server for the share-surface tests: real Postgres, real BlobStore, real sockets, real HTTP client.

The aiohttp server runs on its own event loop in a background thread; tests talk to it with ``http.client`` and seed or
inspect the database with short-lived ``asyncio.run`` calls. No doubles anywhere.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import io
import json
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from herdr_artifact_host.blobs import BlobStore
from herdr_artifact_host.library import LibraryStore
from herdr_artifact_host.public_server import SURFACE, PublicConfig, PublicServer, build_app
from herdr_artifact_host.share_store import token_digest

POOL_ID = "us-west-2_TestPool1"
ISS = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL_ID}"
WEB_CLIENT = "web-client"
CLI_CLIENT = "cli-client"
SECRET = b"k" * 48
PUBLIC_URL = "https://share.example"
COGNITO = "https://auth.lab.example"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

FILES: dict[str, bytes] = {
    "index.html": b"<!doctype html><html><head><title>t</title></head><body><p>hello</p></body></html>",
    "notes.txt": b"plain notes\nline two\n",
    "data/big.bin": bytes(range(256)) * 40,
    "page2.html": b"<html><body>two</body></html>",
}


def jwks_doc(key: rsa.RSAPrivateKey = KEY, kid: str = "k1") -> dict[str, Any]:
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**d, "kid": kid, "alg": "RS256", "use": "sig"}]}


def _encode(claims: dict[str, Any], key: rsa.RSAPrivateKey, kid: str) -> str:
    return jwt.encode(
        {k: v for k, v in claims.items() if v is not None},
        key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def access(
    user: str = "alice",
    *,
    groups: tuple[str, ...] = (),
    client: str = WEB_CLIENT,
    exp: float | None = None,
    key: rsa.RSAPrivateKey = KEY,
    kid: str = "k1",
) -> str:
    claims = {
        "iss": ISS,
        "sub": f"sub-{user}",
        "username": user,
        "token_use": "access",
        "client_id": client,
        "cognito:groups": list(groups),
        "exp": time.time() + 3600 if exp is None else exp,
        "iat": time.time() - 5,
    }
    return _encode(claims, key, kid)


def ident(
    user: str = "alice",
    *,
    email: str = "Alice@Lab.example",
    verified: bool = True,
    aud: str = WEB_CLIENT,
    exp: float | None = None,
    key: rsa.RSAPrivateKey = KEY,
    kid: str = "k1",
) -> str:
    claims = {
        "iss": ISS,
        "sub": f"sub-{user}",
        "cognito:username": user,
        "token_use": "id",
        "aud": aud,
        "email": email,
        "email_verified": verified,
        "exp": time.time() + 3600 if exp is None else exp,
        "iat": time.time() - 5,
    }
    return _encode(claims, key, kid)


@dataclass(frozen=True)
class Resp:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        values = [v for k, v in self.headers if k.lower() == name.lower()]
        return values[0] if values else None

    def json(self) -> Any:
        return json.loads(self.body)

    def shape(self) -> tuple[int, tuple[tuple[str, str], ...], bytes]:
        """Everything that must be byte-identical across uniform failures (the Date header varies by the second)."""
        return (
            self.status,
            tuple(sorted((k.lower(), v) for k, v in self.headers if k.lower() != "date")),
            self.body,
        )


@dataclass(frozen=True)
class Seeded:
    token: str
    share_id: str
    artifact_id: str
    version_id: str


@dataclass
class Host:
    """A running public server plus the means to seed and inspect its database."""

    dsn: str
    blob_root: Path
    static_dir: Path
    sync_dir: Path
    port: int = 0
    bind: str = "127.0.0.1"
    loop: asyncio.AbstractEventLoop | None = None
    server: PublicServer | None = None
    surface: Any = None
    _thread: threading.Thread | None = None
    _pool: asyncpg.Pool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # -- lifecycle

    def start(self, config: PublicConfig) -> None:
        ready = threading.Event()
        failure: list[BaseException] = []

        def main() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop

            async def boot() -> None:
                self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=8)
                app = build_app(
                    config, self._pool, BlobStore(self.blob_root), LibraryStore(self._pool)
                )
                self.surface = app[SURFACE]
                self.server = PublicServer(app)
                self.port = await self.server.start(self.bind)

            try:
                loop.run_until_complete(boot())
            except BaseException as exc:
                failure.append(exc)
                ready.set()
                return
            ready.set()
            loop.run_forever()
            loop.run_until_complete(self._shutdown())
            loop.close()

        self._thread = threading.Thread(target=main, daemon=True)
        self._thread.start()
        ready.wait(30)
        if failure:
            raise failure[0]

    async def _shutdown(self) -> None:
        if self.server is not None:
            await self.server.stop()
        if self._pool is not None:
            await self._pool.close()

    def stop(self) -> None:
        if self.loop is not None and self._thread is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(30)

    # -- database access (a separate short-lived pool, so seeding never touches the server's)

    def db[T](self, body: Callable[[asyncpg.Pool], Any]) -> T:
        async def go() -> T:
            pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=2)
            try:
                return await body(pool)
            finally:
                await pool.close()

        return asyncio.run(go())

    def rows(self, sql: str, *args: object) -> list[dict[str, Any]]:
        async def body(pool: asyncpg.Pool) -> list[dict[str, Any]]:
            return [dict(r) for r in await pool.fetch(sql, *args)]

        return self.db(body)

    def execute(self, sql: str, *args: object) -> None:
        async def body(pool: asyncpg.Pool) -> None:
            await pool.execute(sql, *args)

        self.db(body)

    def events(self, share_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        out = self.rows(
            "SELECT kind, actor, detail::text AS detail FROM share_events WHERE share_id=$1 ORDER BY event_id",
            share_id,
        )
        return [e for e in out if kind is None or e["kind"] == kind]

    # -- blobs and shares

    def put_blob(self, data: bytes) -> str:
        return BlobStore(self.blob_root).put(io.BytesIO(data))

    def seed_share(
        self,
        files: dict[str, bytes] | None = None,
        *,
        audience: str = "public",
        state: str = "live",
        emails: tuple[str, ...] = (),
        groups: tuple[str, ...] = (),
        expires_in: float | None = 3600,
        entry: str | None = "index.html",
        title: str = "Weekly report",
        sharer: str | None = "Dana Doe",
        lifecycle: str = "active",
        intake: str = "accepted",
        version_state: str = "ready",
    ) -> Seeded:
        files = FILES if files is None else files
        manifest = []
        for name, data in files.items():
            manifest.append(
                {
                    "name": name,
                    "sha256": self.put_blob(data),
                    "size_bytes": len(data),
                    "media_type": "application/octet-stream",
                }
            )
        owner = "owner_" + uuid.uuid4().hex[:8]
        aid, vid = "artifact_" + uuid.uuid4().hex, "version_" + uuid.uuid4().hex
        sid, token = "share_" + uuid.uuid4().hex[:24], secrets.token_urlsafe(32)
        expires = None if expires_in is None else datetime.now(UTC) + timedelta(seconds=expires_in)

        async def body(pool: asyncpg.Pool) -> None:
            await pool.execute(
                "INSERT INTO artifacts (artifact_id, owner, created_by, display_name, intake, lifecycle) "
                "VALUES ($1,$2,$2,'A',$3::intake_state,$4)",
                aid,
                owner,
                intake,
                lifecycle,
            )
            await pool.execute(
                "INSERT INTO artifact_versions (version_id, artifact_id, version_number, state, output_manifest) "
                "VALUES ($1,$2,1,$3,$4::jsonb)",
                vid,
                aid,
                version_state,
                json.dumps(manifest),
            )
            await pool.execute(
                "INSERT INTO shares (share_id, artifact_id, version_id, audience, token_hash, allow_emails, "
                "allow_groups, state, entry, title, expires_at, created_by) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                sid,
                aid,
                vid,
                audience,
                token_digest(token),
                list(emails),
                list(groups),
                state,
                entry,
                title,
                expires,
                owner,
            )
            if sharer is not None:
                await pool.execute(
                    "INSERT INTO member_usage (member_id, display_name) VALUES ($1,$2) "
                    "ON CONFLICT (member_id) DO UPDATE SET display_name = EXCLUDED.display_name",
                    owner,
                    sharer,
                )

        self.db(body)
        return Seeded(token, sid, aid, vid)

    # -- HTTP

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | list[tuple[str, str]] | None = None,
        body: bytes | None = None,
    ) -> Resp:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            items = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
            if not any(k.lower() == "host" for k, _ in items):
                items.insert(0, ("Host", f"127.0.0.1:{self.port}"))
            for k, v in items:
                conn.putheader(k, v)
            if body is not None and not any(k.lower() == "content-length" for k, _ in items):
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            r = conn.getresponse()
            data = r.read() if method != "HEAD" else b""
            return Resp(r.status, r.getheaders(), data)
        finally:
            conn.close()

    def get(self, path: str, headers: dict[str, str] | None = None) -> Resp:
        return self.request("GET", path, headers)


def make_sync_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "jwks.json").write_text(json.dumps(jwks_doc()))
    return root


def default_config(static_dir: Path | None, sync_dir: Path, **over: Any) -> PublicConfig:
    base: dict[str, Any] = {
        "public_url": PUBLIC_URL,
        "signing_secret": SECRET,
        "session_seconds": 3600,
        "sync_dir": sync_dir,
        "pool_id": POOL_ID,
        "client_ids": (CLI_CLIENT,),
        "web_client_id": WEB_CLIENT,
        "cognito_domain": COGNITO,
        "static_dir": static_dir,
    }
    base.update(over)
    return PublicConfig(**base)


@contextmanager
def running_host(dsn: str, tmp: Path, config_over: dict[str, Any] | None = None) -> Iterator[Host]:
    from herdr_artifact_host.share_http import DEFAULT_STATIC_DIR

    sync = make_sync_dir(tmp / "sync")
    host = Host(dsn, tmp / "blobs", DEFAULT_STATIC_DIR, sync)
    host.start(default_config(DEFAULT_STATIC_DIR, sync, **(config_over or {})))
    try:
        yield host
    finally:
        host.stop()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
