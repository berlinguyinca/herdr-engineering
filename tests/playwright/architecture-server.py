"""Playwright fixture for the Architecture tab. Snapshots are produced by the REAL analyzer
on a throwaway git repository (and optionally on this checkout), not hand-written JSON.

ARCH_MODE: data (default) | empty | disabled | failed.  ARCH_SELF=1 also publishes this repo's HEAD.
Prints PORT=<n> when ready.
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from herdr_engineering.architecture import analyze  # noqa: E402
from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

RID = "github.com/example/fixture"
FILES = {
    "app/__init__.py": "from .core import run\n",
    "app/core.py": "import os\nfrom . import util\nfrom app.util import helper\n\nclass Engine:\n    def go(self):\n        pass\n\ndef run():\n    return util.helper()\n",
    "app/util.py": "import json\n\ndef helper():\n    return json.dumps({})\n",
    "web/ui.ts": "export const x = 1\n",
}
ENV = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, env=ENV)


def build_fixture(root):
    git(root, "init", "-q", "-b", "main")
    for rel, text in FILES.items():
        p = Path(root, rel); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "one")
    first = analyze.analyze_commit(str(root), "HEAD", "beast", RID)
    Path(root, "app/util.py").write_text("import json\n\ndef helper():\n    return 2\n\ndef added():\n    pass\n")
    Path(root, "app/new.py").write_text("from app import core\n")
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "two")
    return first, analyze.analyze_commit(str(root), "HEAD", "beast", RID)


class FakeLLM(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        facts = json.loads(body["messages"][1]["content"].split("\n", 1)[1])
        real = facts["edges"][0]["id"] if facts["edges"] else "e_" + "0" * 20
        text = f"This node is imported by a sibling [{real}] and by a made-up edge [e_ffffffffffffffffffff]."
        out = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


def seed_activity(svr, repo, head):
    from types import SimpleNamespace
    repo.upsert_session("pi:s1", host_id="beast", agent_role="implementer", source="pi", repository_id=RID, commit_sha=head, parent_session_id="pi:root")
    repo.upsert_session("branchy", host_id="beast", repository_id=RID, branch="main", cwd="/x")
    repo.observe_ci_pipeline({"provider_repo_id": "9", "repository": "fixture", "run_number": 5, "state": "success", "branch": "main",
                              "repository_id": RID, "commit_sha": head, "url": "https://ci.example/5"})
    wt = next(m["worktree_id"] for m in svr.architecture.list_snapshots(RID, None, None, None, None)["snapshots"]
              if m["commit_sha"] == head)
    repo.upsert_session("pi:same-tree", host_id="beast", agent_role="reviewer", source="pi", repository_id=RID,
                        commit_sha="a" * 40, worktree_id=wt)
    gate = SimpleNamespace(snapshots=lambda: [SimpleNamespace(key=SimpleNamespace(repo=RID, number=17), head_sha=head,
                                                              state=SimpleNamespace(value="ci_pending"))])
    svr.architecture.gate_store_fn = lambda: gate


def main():
    mode = os.environ.get("ARCH_MODE", "data")
    repo = ControlPlaneRepo()
    explain = None
    if mode == "data":
        llm = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=llm.serve_forever, daemon=True).start()
        explain = {"enabled": True, "url": f"http://127.0.0.1:{llm.server_address[1]}/v1/chat/completions", "model": "fake"}
    svr = ControlPlaneServer(repo, architecture_enabled=mode != "disabled", architecture_explain=explain)
    if mode in {"data", "failed"}:
        tmp = tempfile.mkdtemp(prefix="arch-fixture-")
        first, second = build_fixture(tmp)
        time.sleep(1.1)  # created_at has 1s resolution: make the second snapshot the newer one
        second["snapshot"]["created_at"] = "2099-01-01T00:00:00+00:00"
        from herdr_engineering.architecture import identity
        for s in (first, second):
            s["snapshot"]["graph_hash"] = identity.graph_hash(s["graph"])
            svr.architecture.publish(s)
        if mode == "data":
            seed_activity(svr, repo, second["snapshot"]["commit_sha"])
            lib = Path(tmp, "..", "arch-lib-" + str(os.getpid())).resolve(); lib.mkdir()
            git(lib, "init", "-q", "-b", "main"); Path(lib, "fixturelib").mkdir()
            Path(lib, "fixturelib/__init__.py").write_text("def f():\n    pass\n")
            git(lib, "add", "-A"); git(lib, "commit", "-q", "-m", "lib")
            svr.architecture.publish(analyze.analyze_commit(str(lib), "HEAD", "beast", "github.com/example/lib"))
            Path(tmp, "app/uses.py").write_text("import fixturelib\n")
            git(tmp, "add", "-A"); git(tmp, "commit", "-q", "-m", "three")
            third = analyze.analyze_commit(str(tmp), "HEAD", "beast", RID)
            third["snapshot"]["created_at"] = "2099-06-01T00:00:00+00:00"
            third["snapshot"]["graph_hash"] = identity.graph_hash(third["graph"])
            svr.architecture.publish(third)
            # a host paired the newest snapshot with its merge-base (the first snapshot) for a PR base request
            req = svr.architecture.request_analysis({"repository_id": RID, "host_id": "beast", "rev": third["snapshot"]["commit_sha"],
                                                     "base_rev": "main"})
            svr.architecture.record_analysis({"analysis_id": req["analysis_id"], "event": "completed", "host_id": "beast",
                                              "commit_sha": third["snapshot"]["commit_sha"], "snapshot_id": third["snapshot"]["snapshot_id"],
                                              "base_commit_sha": first["snapshot"]["commit_sha"], "base_snapshot_id": first["snapshot"]["snapshot_id"]})
            seed_activity(svr, repo, third["snapshot"]["commit_sha"])
        if os.environ.get("ARCH_SELF") == "1":
            svr.architecture.publish(analyze.analyze_commit(str(_REPO), "HEAD", "bender",
                                                           "github.com/berlinguyinca/herdr-engineering"))
    if mode == "failed":
        svr.architecture.record_analysis({"analysis_id": "an-bad", "event": "failed", "message": "analysis exceeded the 300s wall-clock limit"})
        svr.architecture.record_analysis({"analysis_id": "an-run", "event": "progress", "progress": 0.4})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    print(f"PORT={svr.port}", flush=True)
    while True:
        time.sleep(3600)


main()
