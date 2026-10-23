"""Playwright fixture for the Architecture tab. Snapshots are produced by the REAL analyzer
on a throwaway git repository (and optionally on this checkout), not hand-written JSON.

ARCH_MODE: data (default) | empty | disabled | failed.  ARCH_SELF=1 also publishes this repo's HEAD.
Prints PORT=<n> when ready.
"""
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


def main():
    mode = os.environ.get("ARCH_MODE", "data")
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, architecture_enabled=mode != "disabled")
    if mode in {"data", "failed"}:
        tmp = tempfile.mkdtemp(prefix="arch-fixture-")
        first, second = build_fixture(tmp)
        time.sleep(1.1)  # created_at has 1s resolution: make the second snapshot the newer one
        second["snapshot"]["created_at"] = "2099-01-01T00:00:00+00:00"
        for s in (first, second):
            from herdr_engineering.architecture import identity
            s["snapshot"]["graph_hash"] = identity.graph_hash(s["graph"])
            svr.architecture.publish(s)
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
