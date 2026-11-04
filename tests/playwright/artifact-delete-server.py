"""Loopback control plane for the dashboard's owner delete flow (Playwright): production handlers over a REAL PostgreSQL.

``ControlPlaneServer`` runs with the real ``ArtifactStore`` on an isolated, throw-away PostgreSQL cluster (initdb in a
temp directory, the repo's migrations applied). Only the object store is in memory (``FakeBlobs``, the registry tests'
stand-in for RustFS). The argument is a JSON mapping on argv (optional): ``{"keys": true}`` starts with API keys
(``owner-key`` is the owner, ``viewer-key`` a read-only viewer) so a non-owner can be tested; otherwise the listener is
open and its operator is the owner.

Prints one JSON line on stdout once ready: ``{"base": ..., "state": ..., "ids": {...}}``. ``state`` is a second tiny
loopback server answering ``GET /`` with ``{"blobs": {<artifact_id>: <bytes still stored>}}`` so a test can prove bytes
really were deleted or kept; ``POST /`` closes the store's database connection so a test can see a dead store.
"""
import asyncio
import hashlib
import http.server
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter  # noqa: E402
from herdr_engineering.control_plane.artifacts import ArtifactStore  # noqa: E402
from herdr_engineering.control_plane.db import Migrator  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402
from tests.control_plane.registry_pg import FakeBlobs  # noqa: E402

NAMES = ["alpha.md", "beta.md", "gamma.md", "hostile \"><img src=x onerror=window.__x=1>.md", "delta.md"]


def start_postgres(area: Path) -> tuple[str, callable]:
    config = shutil.which("pg_config")
    candidates = []
    if config:
        candidates.append(Path(subprocess.check_output([config, "--bindir"], text=True).strip()) / "initdb")
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"), reverse=True)
    initdb = next(c for c in candidates if c.is_file())
    bindir, data = initdb.parent, area / "data"
    subprocess.run([str(initdb), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "--no-sync"],
                   check=True, capture_output=True)
    subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-l", str(area / "server.log"), "-o",
                    f"-c listen_addresses='' -k {area} -c dynamic_shared_memory_type=mmap", "-w", "start"],
                   check=True, capture_output=True)

    def stop():
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"], capture_output=True)

    dsn = f"postgresql:///postgres?host={area}"

    async def migrate():
        conn = await asyncpg.connect(dsn)
        try:
            await Migrator(conn).apply(str(ROOT / "deploy/control-plane/migrations"))
        finally:
            await conn.close()

    asyncio.run(migrate())
    return dsn, stop


def main() -> None:
    options = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    area = Path(tempfile.mkdtemp(prefix="herdr-delete-fixture-"))
    dsn, stop_pg = start_postgres(area)
    blobs = FakeBlobs()

    holder = {}

    async def make():
        holder["conn"] = await asyncpg.connect(dsn)
        return ArtifactStore(holder["conn"], blobs)

    adapter = SyncArtifactAdapter(make)
    ids, keys_of = {}, {}
    keyed = bool(options.get("keys"))
    # The dashboard lists what its own key created: seed as that principal (an open listener hashes the empty key).
    creator = f"api-key:{hashlib.sha256(b'owner-key' if keyed else b'').hexdigest()[:16]}:operator"
    for name in NAMES:
        data = f"fixture bytes for {name}".encode()
        art = adapter("register_logical", original_filename=name, created_by=creator, mime_type="text/markdown")
        key, sha = blobs.put(data)
        adapter("commit_version", art["artifact_id"], base_version_id=None, source_manifest_blob=None, created_by=creator,
                output_manifest=[{"name": name, "storage_key": key, "sha256": sha, "size_bytes": len(data),
                                  "media_type": "text/markdown"}])
        ids[name], keys_of[art["artifact_id"]] = art["artifact_id"], key
    owner_key, viewer_key = "owner-key", "viewer-key"
    server = ControlPlaneServer(
        ControlPlaneRepo(), host="127.0.0.1", port=0, web_root=ROOT / "packages", artifact_store=adapter,
        api_keys={owner_key: "operator", viewer_key: "viewer"} if keyed else None,
        artifact_delete_owners=frozenset({hashlib.sha256(owner_key.encode()).hexdigest()[:16]}))
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()

    class State(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            payload = json.dumps({"blobs": {a: blobs.head(k) for a, k in keys_of.items()}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):               # close the store's database connection: the next owner call is a dead store
            asyncio.run_coroutine_threadsafe(holder["conn"].close(), adapter._loop).result()
            self.send_response(204)
            self.end_headers()

    state = http.server.ThreadingHTTPServer(("127.0.0.1", 0), State)
    threading.Thread(target=state.serve_forever, daemon=True).start()

    def cleanup(*_args):
        server.shutdown()
        adapter.close()
        stop_pg()
        shutil.rmtree(area, ignore_errors=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    print(json.dumps({"base": f"http://127.0.0.1:{server.port}", "state": f"http://127.0.0.1:{state.server_port}",
                      "ids": ids}), flush=True)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
