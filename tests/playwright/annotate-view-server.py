"""A real ``share-public`` with an annotation-enabled lab share, behind a TLS front, for the annotation-view browser test.

Usage: annotate-view-server.py <listener-port>. Hostile bundle files point at that loopback listener so the test can
prove no request leaves. The server's configured origin is ``https://share.example`` (the CSP names it), so the test
maps that host to the TLS front with ``--host-resolver-rules``. Stdout: one JSON line with port, token and session.
"""
import json
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.control_plane.test_share_annotate import TOKEN, make_env, session  # noqa: E402

LISTENER = int(sys.argv[1])
EVIL = f"http://127.0.0.1:{LISTENER}"
HOST_RULE = ":host{position:fixed!important;inset:0;z-index:2147483647;transform:scale(3);filter:blur(1px);" \
            "mix-blend-mode:difference;display:block;opacity:.9;background:red}"
COVER = "position:absolute;inset:-9999px;z-index:2147483647;transform:translateZ(0);filter:blur(1px);" \
        "mix-blend-mode:difference;background:rgba(0,0,255,.5)"
FILES = {
    "index.html": b"<p>ok</p>",
    "wide.txt": ("wide " * 400).encode(),
    "host.html": f"<style>{HOST_RULE}</style><p>host</p><script>window.__x=1;alert(1)</script>".encode(),
    "abs.html": f"<style>div{{{COVER}}} p{{position:fixed;top:0;left:0;width:100vw;height:100vh;z-index:2147483647}}"
                f" p{{background:url({EVIL}/bg.png)}}</style><div>c</div><p onclick=alert(2)>x</p>"
                f"<img src='{EVIL}/img.png' onerror=alert(3)><a href=\"javascript:alert(4)\">j</a>".encode(),
    "xform.html": (f"<style>:host{{transform:scale(5) rotate(3deg);filter:invert(1);will-change:transform;"
                   f"isolation:auto;contain:none;overflow:visible}} p{{position:fixed;inset:0;z-index:99999;"
                   f"transform:translate(0,0)}} @import url({EVIL}/imp.css);</style><p>y</p>").encode(),
}


def tls_front(target_port: int, certdir: Path) -> int:
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=share.example",
                    "-addext", "subjectAltName=DNS:share.example", "-keyout", str(certdir / "k.pem"),
                    "-out", str(certdir / "c.pem")], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certdir / "c.pem", certdir / "k.pem")
    listener = socket.create_server(("127.0.0.1", 0))
    listener.listen(64)

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
            upstream = socket.create_connection(("127.0.0.1", target_port))
        except (OSError, ssl.SSLError):
            raw.close()
            return
        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pump, args=(upstream, client), daemon=True).start()

    def accept():
        while True:
            conn, _ = listener.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return listener.getsockname()[1]


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="annview"))
    env = make_env(tmp, files=FILES)
    front = tls_front(env.port, tmp)
    print(json.dumps({"port": front, "token": TOKEN, "session": session()}), flush=True)
    threading.Event().wait()


main()
