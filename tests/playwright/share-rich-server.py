"""Fixture for share-rich-view.test.mjs: a real owner API makes an opted-in report share, a real share host serves it.

Reuses the in-memory registry/share store of tests/control_plane/test_artifact_share_api.py (the HTTP handlers, bundle
building and the share host are the production code). Prints ``PORT=<share host port>`` and ``PATH=/s/<token>/``;
the token is a throwaway of this temporary run.
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from herdr_engineering.control_plane.share_public import (  # noqa: E402
    ShareConfig,
    make_share_server,
)
from tests.control_plane.test_artifact_share_api import (  # noqa: E402
    approve,
    create,
    share_server,
    token_of,
)
from tests.control_plane.test_share_rich_view import add_version  # noqa: E402

PAGE = b"""<!doctype html><html><head><title>Author page</title><style>
body{font:16px/1.5 Georgia,serif;margin:0;background:#fdf6e3;color:#222}main{max-width:40rem;margin:0 auto;padding:2rem 1rem}
h1{color:#8a2be2}.k{display:inline-block;padding:.2rem .6rem;background:#8a2be2;color:#fff;border-radius:.4rem}
@media (prefers-color-scheme: dark){body{background:#1e1b2e;color:#eee}}</style></head><body><main>
<h1>Candidate cuts p95 latency by 18%</h1><p>Measured over <span class="k">312 ms</span> median.</p>
<p id="probe">Scripts have not run.</p>
<script>document.getElementById('probe').textContent='Scripts RAN';try{window.top.document.title='pwned'}catch(e){}</script>
</main></body></html>"""


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp, share_server(Path(tmp)) as (base, registry, _shares, blobs, root):
        sha = blobs.add(PAGE)
        asset = {"id": "page", "name": "report.html", "role": "preview", "media_type": "text/html", "sha256": sha,
                 "size_bytes": len(PAGE)}
        vid = add_version(registry, blobs, asset)
        status, made = create(base, version=vid, include_author_html=True)
        assert status == 201, made
        status, done = approve(base, made["share"]["share_id"])
        assert status == 200, done
        token = token_of(done["url"])
        events = Path(tmp) / "events"
        events.mkdir()
        server = make_share_server(ShareConfig(shares_root=root, events_path=events / "e.jsonl"), host="127.0.0.1", port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"PORT={server.server_address[1]}", flush=True)
        print(f"PATH=/s/{token}/", flush=True)
        json.dumps({})
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
