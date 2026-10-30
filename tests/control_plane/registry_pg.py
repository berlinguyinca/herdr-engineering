"""Shared fixtures: an isolated, socket-only PostgreSQL with every control-plane migration applied, and a fake RustFS."""
import asyncio
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import asyncpg
import pytest

from herdr_engineering.control_plane.db import Migrator
from herdr_engineering.control_plane.rustfs import IntegrityError

ROOT = Path(__file__).resolve().parents[2]


class FakeBlobs:
    """Content-addressed object store with the RustFSClient surface the registry uses."""

    def __init__(self):
        self.objects = {}
        self.deleted = []

    @staticmethod
    def key(digest):
        return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        self.objects[self.key(digest)] = bytes(data)
        return self.key(digest), digest

    def put_at(self, key, data):
        assert key.startswith("staging/")
        self.objects[key] = bytes(data)
        return key

    def put_stream(self, stream, *, expected_sha256=None, expected_size=None, media_type=None, chunk_size=1 << 20):
        data = io.BytesIO()
        while chunk := stream.read(chunk_size):
            data.write(chunk)
        body = data.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        if expected_size is not None and len(body) != expected_size:
            raise IntegrityError("size mismatch")
        if expected_sha256 is not None and digest != expected_sha256:
            raise IntegrityError("sha256 mismatch")
        self.objects[self.key(digest)] = body
        return {"key": self.key(digest), "sha256": digest, "size": len(body), "media_type": media_type}

    def iter_get(self, key, expected_sha256, *, chunk_size=1 << 20):
        body = self.objects[key]
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise IntegrityError("sha256 mismatch")
        for offset in range(0, len(body), chunk_size):
            yield body[offset:offset + chunk_size]

    def get(self, key, expected_sha256):
        return b"".join(self.iter_get(key, expected_sha256))

    def head(self, key):
        return key in self.objects

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)

    def staging(self):
        return [k for k in self.objects if k.startswith("staging/")]


@pytest.fixture(scope="module")
def registry_pg():
    if os.getuid() == 0:
        pytest.skip("isolated initdb cannot run as root")
    config = shutil.which("pg_config")
    candidates = []
    if config:
        candidates.append(Path(subprocess.check_output([config, "--bindir"], text=True).strip()) / "initdb")
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"), reverse=True)
    initdb = next((c for c in candidates if c.is_file()), None)
    if initdb is None:
        pytest.skip("PostgreSQL server binaries unavailable")
    bindir = initdb.parent
    with tempfile.TemporaryDirectory(prefix="registry-pg-") as folder:
        area = Path(folder)
        data = area / "data"
        subprocess.run([str(initdb), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "--no-sync"],
                       check=True, capture_output=True)

        def pg_ctl(*args):
            subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), *args], check=True, capture_output=True)

        def start():
            pg_ctl("-l", str(area / "server.log"), "-o", f"-c listen_addresses='' -k {area}", "-w", "start")

        def stop():
            pg_ctl("-m", "fast", "-w", "stop")

        def restart():
            stop()
            start()

        start()
        try:
            dsn = f"postgresql:///postgres?host={area}"

            async def initialize():
                connection = await asyncpg.connect(dsn)
                try:
                    await Migrator(connection).apply(str(ROOT / "deploy/control-plane/migrations"))
                finally:
                    await connection.close()

            asyncio.run(initialize())
            yield {"dsn": dsn, "restart": restart}
        finally:
            stop()


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
