"""A real ``share-public`` with lab and public shares, annotations enabled, behind a TLS front, for share-annotations.test.mjs.

Usage: share-annotations-server.py <external-listener-port>. The hostile bundle file points at that loopback listener so
the test can prove no request leaves. The share server's configured origin is ``https://127.0.0.1:<front-port>``, so the
browser needs no host mapping (works in Chromium, Firefox and WebKit with ``ignoreHTTPSErrors``).

Stdout: one line ``PORT=<front-port>``, then one JSON line with the origin, the control URL, the share tokens and a
default signed-in session per lab share (``v/``, ``a/`` and ``f/`` base URLs) plus the public URL.

The control server (plain HTTP, 127.0.0.1, NOT part of the share server):
  GET  /__lines                  the parsed annotations.jsonl lines
  POST /__signin/<share>/<user>  a real session exchange (signed Cognito-shaped tokens) -> {landing, base}
  POST /__project                the worker's tick: fold the lines into each viewer's projection file
  POST /__status/<id>/<status>   mark a projected annotation dismissed or addressed (the owner's side)
  POST /__revoke/<share>         remove the share's state (the very next request is the uniform 404)
  POST /__break, /__fix          make the annotations file unwritable / writable again (a real 503)
"""
import hashlib
import json
import re
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import zlib
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from herdr_engineering.control_plane.annotation_anchor import annotation_settings  # noqa: E402
from herdr_engineering.control_plane.share_bundle import (  # noqa: E402
    BundleLimits,
    build_bundle,
    remove_state,
    write_own_projection,
    write_state,
)
from herdr_engineering.control_plane.share_public import ShareConfig, make_share_server  # noqa: E402
from tests.control_plane.test_share_lab import access, ident, lab_config  # noqa: E402

LISTENER = int(sys.argv[1]) if len(sys.argv) > 1 else 9
EVIL_ORIGIN = f"http://127.0.0.1:{LISTENER}"
FRAME = "<" + "iframe"      # the literal tag is never written out in this repo
MAX_COMMENT = 300


def make_pdf() -> bytes:
    """A real three-page PDF (standard Helvetica, text only) with a correct cross-reference table."""
    texts = ["Page one alpha text", "Page two beta text", "Page three target sentence here"]
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for i, text in enumerate(texts):
        page_no, content_no = 4 + 2 * i, 5 + 2 * i
        kids.append(f"{page_no} 0 R")
        stream = f"BT /F1 24 Tf 60 700 Td ({text}) Tj ET".encode()
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_no} 0 R "
                       f"/Resources << /Font << /F1 3 0 R >> >> >>".encode())
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects[1] = f"<< /Type /Pages /Count {len(texts)} /Kids [{' '.join(kids)}] >>".encode()
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def make_png(width: int = 200, height: int = 120) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + bytes(v for x in range(width) for v in (x * 255 // width, y * 255 // height, 128))
                    for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


INDEX = ("<!doctype html><html><body><h1>Quarterly notes</h1>"
         "<p id=first>The quick brown fox jumps over the lazy dog. A second sentence follows here.</p>"
         "<p id=span>Alpha <b>bold</b> and <i>italic</i> end of line.</p>"
         "<p id=rtl dir=rtl>مرحبا بالعالم text שלום</p>"
         "<p id=emoji>Party \U0001F389\U0001F600 time \U0001F468‍\U0001F469‍\U0001F467 done.</p>"
         "<p id=cr>carriage&#13;return line</p>"
         "<p id=huge>" + " ".join(f"word{i}" for i in range(700)) + "</p>"
         "<p><a id=next href='page2.html'>second page</a></p>"
         "<script>var m=document.createElement('p');m.id='marker';m.textContent='live script ran';document.body.append(m);</script>"
         "</body></html>")
COVER = ("<!doctype html><html><head><style>:host{position:fixed!important;inset:0;z-index:2147483647;transform:scale(3);"
         "filter:blur(1px);mix-blend-mode:difference;background:red}div,p{position:fixed;inset:0;z-index:2147483647;"
         "transform:translateZ(0);background:rgba(0,0,255,.5)}</style></head><body><div>cover</div><p>Cover text sentence.</p>"
         "</body></html>")
PAGE2 = "<!doctype html><html><body><p id=two>This is the second page with its own sentence.</p></body></html>"
EVIL = ("<!doctype html><html><head><style>* { display: none !important }</style></head><body>"
        "<script>window.__x=1;alert('evil script')</script>"
        f"{FRAME} src='{EVIL_ORIGIN}/frame'></" + "iframe>"
        f"<img src='{EVIL_ORIGIN}/img.png' onerror=\"window.__x=2;alert('onerror')\">"
        "<div id=annotate-panel data-annotate-panel><button onclick=\"alert('panel')\">fake</button></div>"
        "<meta name=herdr-annotate-csrf content=evil>"
        "<div style='position:fixed;inset:0;z-index:2147483647;background:red'>overlay</div>"
        "<p id=evilp onclick=\"alert('click')\">Hostile paragraph with a selectable sentence.</p></body></html>")

SHARES: dict[str, dict] = {
    "html": {"id": "shr-html", "token": "H" * 43, "audience": "lab", "entry": "index.html",
             "files": {"index.html": INDEX.encode(), "page2.html": PAGE2.encode(), "evil.html": EVIL.encode(),
                       "cover.html": COVER.encode()}},
    "pdf": {"id": "shr-pdf", "token": "P" * 43, "audience": "lab", "entry": "report.pdf",
            "files": {"report.pdf": make_pdf()}},
    "png": {"id": "shr-png", "token": "G" * 43, "audience": "lab", "entry": "pic.png",
            "files": {"pic.png": make_png()}},
    "par": {"id": "shr-par", "token": "Q" * 43, "audience": "lab", "entry": "data.parquet",
            "files": {"data.parquet": bytes(range(256)) * 8}},
    "pub": {"id": "shr-pub", "token": "U" * 43, "audience": "public", "entry": "index.html",
            "files": {"index.html": b"<!doctype html><html><body><p>public page</p></body></html>"}},
}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def reserve_front(certdir: Path):
    """The TLS front's listener first (the share's configured origin names its port), wired to the share port later."""
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(certdir / "k.pem"),
                    "-out", str(certdir / "c.pem")], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certdir / "c.pem", certdir / "k.pem")
    listener = socket.create_server(("127.0.0.1", 0))
    listener.listen(64)
    target: list[int] = []

    def pump(src, dst):
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except (OSError, ssl.SSLError):
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def handle(raw):
        try:
            client = ctx.wrap_socket(raw, server_side=True)
            upstream = socket.create_connection(("127.0.0.1", target[0]))
        except (OSError, ssl.SSLError, IndexError):
            raw.close()
            return
        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pump, args=(upstream, client), daemon=True).start()

    def accept():
        while True:
            conn, _ = listener.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return listener.getsockname()[1], target


class Harness:
    def __init__(self, tmp: Path, front_port: int):
        self.tmp = tmp
        self.root = tmp / "shares"
        self.events = tmp / "events" / "events.jsonl"
        self.events.parent.mkdir()
        self.origin = f"https://127.0.0.1:{front_port}"
        self.status: dict[str, dict] = {}
        for share in SHARES.values():
            lab = share["audience"] == "lab"
            expiry = (datetime.now(UTC) + timedelta(days=1)).isoformat()
            index = build_bundle(self.root / share["id"],
                                 [(n, hashlib.sha256(b).hexdigest(), len(b), [b]) for n, b in share["files"].items()],
                                 entry=share["entry"], limits=BundleLimits(1 << 24, 100))
            state = {"share_id": share["id"], "audience": share["audience"], "state": "live", "expires_at": None if lab else expiry,
                     "allow_emails": [], "allow_groups": ["Readers"] if lab else [], "entry": index.entry,
                     "title": share["id"],
                     "files": [{"name": f.name, "sha256": f.sha256, "size": f.size, "content_type": f.content_type}
                               for f in index.files]}
            write_state(self.root, token_hash(share["token"]), state)
            share["state"] = state
        config = lab_config(tmp)
        config["public_url"] = self.origin
        limits = annotation_settings({"max_comment_chars": MAX_COMMENT, "per_user_per_minute": 60})
        self.config = ShareConfig(shares_root=self.root, events_path=self.events, annotations=limits, **config)
        self.server = make_share_server(self.config, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def lines_path(self) -> Path:
        return self.config.annotations_path

    def lines(self) -> list[dict]:
        path = self.lines_path
        return [json.loads(x) for x in path.read_bytes().splitlines()] if path.exists() else []

    def signin(self, share: str, user: str) -> dict:
        token = SHARES[share]["token"]
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", f"/s/{token}/session", headers={
                "Authorization": "Bearer " + access(user, groups=("Readers",)),
                "X-Id-Token": ident(user, email=f"{user}@lab.example"), "Content-Length": "0"})
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        if resp.status != 200:
            raise RuntimeError(f"sign-in refused: {resp.status}")
        landing = json.loads(body)["landing"]
        match = re.match(r"(/s/[^/]+/k/[^/]+/)", landing)
        if match is None:
            raise RuntimeError("unexpected landing")
        return {"landing": landing, "base": self.origin + match.group(1)}

    def project(self) -> None:
        """The worker's tick (the real one needs PostgreSQL): fold the lines into one projection file per viewer."""
        rows: dict[tuple[str, str], dict[str, dict]] = {}
        for line in self.lines():
            key = (line["share_id"], line["author_identity"])
            mine = rows.setdefault(key, {})
            aid = line["annotation_id"]
            if line["op"] == "create":
                mine[aid] = {"annotation_id": aid, "file": line["anchor"]["file"], "anchor": line["anchor"],
                             "body": line["body"], "status": "open", "version_number": 1,
                             "addressed_in_version_number": None, "created_at": line["at"], "edited_at": None}
            elif aid in mine and mine[aid]["status"] == "open":
                if line["op"] == "edit":
                    mine[aid].update(body=line["body"], edited_at=line["at"])
                elif line["op"] == "delete":
                    del mine[aid]
        for (share_id, identity), mine in rows.items():
            for aid, row in mine.items():
                row.update(self.status.get(aid, {}))
            ordered = sorted(mine.values(), key=lambda r: r["created_at"], reverse=True)
            write_own_projection(self.root, share_id, identity, ordered)

    def revoke(self, share: str) -> None:
        remove_state(self.root, token_hash(SHARES[share]["token"]))


def control_handler(h: Harness):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, payload: object, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(h.lines() if self.path == "/__lines" else {"error": "unknown"}, 200 if self.path == "/__lines" else 404)

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            try:
                if parts[0] == "__signin" and len(parts) == 3:
                    return self._send(h.signin(parts[1], parts[2]))
                if parts[0] == "__project":
                    h.project()
                    return self._send({"ok": True})
                if parts[0] == "__status" and len(parts) == 3:
                    h.status[parts[1]] = ({"status": "dismissed"} if parts[2] == "dismissed"
                                          else {"status": "addressed", "addressed_in_version_number": 2})
                    h.project()
                    return self._send({"ok": True})
                if parts[0] == "__revoke" and len(parts) == 2:
                    h.revoke(parts[1])
                    return self._send({"ok": True})
                if parts[0] == "__break":
                    h.lines_path.touch()
                    h.lines_path.chmod(0o400)
                    return self._send({"ok": True})
                if parts[0] == "__fix":
                    h.lines_path.touch()
                    h.lines_path.chmod(0o644)
                    return self._send({"ok": True})
            except Exception as exc:    # a test harness: report, never hang the test
                return self._send({"error": type(exc).__name__ + ": " + str(exc)}, 500)
            return self._send({"error": "unknown"}, 404)

    return Handler


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="shareann"))
    front_port, target = reserve_front(tmp)
    harness = Harness(tmp, front_port)
    target.append(harness.port)
    control = ThreadingHTTPServer(("127.0.0.1", 0), control_handler(harness))
    threading.Thread(target=control.serve_forever, daemon=True).start()
    info = {"origin": harness.origin, "control": f"http://127.0.0.1:{control.server_address[1]}",
            "tokens": {k: v["token"] for k, v in SHARES.items()}, "maxComment": MAX_COMMENT, "sessions": {}}
    for name, share in SHARES.items():
        if share["audience"] == "lab":
            info["sessions"][name] = harness.signin(name, "alice")
    info["public"] = f"{harness.origin}/s/{SHARES['pub']['token']}/"
    print(f"PORT={front_port}", flush=True)
    print(json.dumps(info), flush=True)
    threading.Event().wait()


main()
