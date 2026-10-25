import os
import subprocess

REPO_ID = "github.com/example/fixture"


def git(cwd, *args, env=None):
    e = {"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
         **(env or {})}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=e).stdout.decode().strip()


def write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


BASE = {
    "app/__init__.py": "from .core import run\n",
    "app/core.py": "import os\nfrom . import util\nfrom app.util import helper\n\nclass Engine:\n    def go(self):\n        pass\n\n"
                   "def run():\n    return util.helper()\n",
    "app/util.py": "import json\n\ndef helper():\n    return json.dumps({})\n",
    "README.md": "x\n",
}


