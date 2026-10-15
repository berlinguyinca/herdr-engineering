import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from herdr_engineering.cli import main


def test_llm_env_writes_0600_file_and_never_prints_the_key(tmp_path, capsys):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            out = json.dumps({"key_id": "k1", "project": "p", "key": "hk-secret"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = tmp_path / "e.env"
    rc = main(["llm-env", "--project", "p", "--control-plane", f"http://127.0.0.1:{srv.server_address[1]}",
               "--out", str(out)])
    srv.shutdown()
    assert rc == 0
    assert "hk-secret" not in capsys.readouterr().out
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    assert "OPENAI_API_KEY=hk-secret" in out.read_text()
