"""The dashboard library, for library-dashboard.test.mjs (Playwright): a REAL artifact host over a real PostgreSQL.

Real: an isolated PostgreSQL (initdb/pg_ctl, unix socket only) migrated by the artifact host's own migrator, the host's
public server (``running_host``) and its internal read listener on a real port (the production aiohttp app, a
``dashboard-read`` bearer key), and a ``ControlPlaneServer`` whose ``library`` is the production ``ArtifactHostClient``.
Nothing is replaced.  Two members' artifacts are seeded; one page contains a script that would rewrite its own text if
it ever ran, so the test can prove the sandbox keeps it inert.

Stdout: ``PORT=<dashboard port>`` and ``IDS=<json of the seeded ids>``.  Stop with SIGTERM.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from herdr_artifact_host.blobs import BlobStore  # noqa: E402
from herdr_artifact_host.db import Migrator, create_pool  # noqa: E402
from herdr_artifact_host.internal_server import (  # noqa: E402
    InternalConfig,
    InternalServer,
    build_app,
    key_digest,
)
from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.artifact_host_client import ArtifactHostClient  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402
from tests.artifact_host.seed import running_host  # noqa: E402

HOST_KEY = "playwright-dashboard-read-" + "k" * 24
PAGE = (b"<!doctype html><html><head><title>Member page</title></head><body><p id=t>static text</p>"
        b"<script>document.getElementById('t').textContent='SCRIPT RAN'</script></body></html>")


def find_initdb() -> Path:
    config = shutil.which("pg_config")
    candidates = [Path(subprocess.check_output([config, "--bindir"], text=True).strip()) / "initdb"] if config else []
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"), reverse=True)
    found = next((c for c in candidates if c.is_file()), None)
    if found is None:
        raise SystemExit("PostgreSQL server binaries unavailable")
    return found


def main() -> None:
    initdb = find_initdb()
    work = Path(tempfile.mkdtemp(prefix="library-dash-"))
    data = work / "data"
    subprocess.run([str(initdb), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "--no-sync"],
                   check=True, capture_output=True)
    subprocess.run([str(initdb.parent / "pg_ctl"), "-D", str(data), "-l", str(work / "pg.log"), "-o",
                    f"-c listen_addresses='' -k {work} -c dynamic_shared_memory_type=mmap", "-w", "start"],
                   check=True, capture_output=True)
    name = "h_" + uuid.uuid4().hex[:12]
    dsn = f"postgresql:///{name}?host={work}"

    async def prepare() -> None:
        conn = await asyncpg.connect(f"postgresql:///postgres?host={work}")
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()
        pool = await create_pool(dsn)
        try:
            await Migrator(pool).migrate()
        finally:
            await pool.close()

    asyncio.run(prepare())
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        with running_host(dsn, work / "host") as host:
            ids = {}
            for label, title, files in (("page", "Quarterly findings", {"index.html": PAGE, "notes.txt": b"plain notes\n"}),
                                        ("other", "Second report", {"index.html": b"<html><body>two</body></html>"})):
                seeded = host.seed_share(files, title=title)
                host.execute("UPDATE artifacts SET display_name=$2, current_version_id=$3 WHERE artifact_id=$1",
                             seeded.artifact_id, title, seeded.version_id)
                ids[label] = {"artifact": seeded.artifact_id, "version": seeded.version_id}

            async def boot() -> InternalServer:
                server = InternalServer(build_app(InternalConfig(key_digest(HOST_KEY), key_failures_per_minute=100_000),
                                                  host._pool, BlobStore(host.blob_root)))
                await server.start()
                return server

            assert host.loop is not None
            internal = asyncio.run_coroutine_threadsafe(boot(), host.loop).result(30)
            dashboard = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, web_root=ROOT / "packages",
                                           library=ArtifactHostClient(f"http://127.0.0.1:{internal.port}", HOST_KEY, timeout=5))
            threading.Thread(target=dashboard.serve, daemon=True).start()
            dashboard.wait_until_ready()
            print(f"IDS={json.dumps(ids)}", flush=True)
            print(f"PORT={dashboard.port}", flush=True)
            stop.wait()
            dashboard.shutdown()
            asyncio.run_coroutine_threadsafe(internal.stop(), host.loop).result(30)
    finally:
        subprocess.run([str(initdb.parent / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
