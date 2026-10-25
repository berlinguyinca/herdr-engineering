import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import BASE, git, write  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, BASE)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root
