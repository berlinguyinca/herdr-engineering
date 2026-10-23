# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Runner and proxy fixes from the final review: no profile fallback, IPv6 requests, claim back-off, the
independent helper watch, fail-closed allow files, live profile refresh, and tunnels cut when a host goes."""
from __future__ import annotations

import pytest

from tests.control_plane.test_collab_spawn import WORKER, runner


@pytest.fixture
def bare(tmp_path):
    def api(method, path, body=None):
        raise AssertionError(f"unexpected call {method} {path}")
    r = runner.Runner("http://127.0.0.1:9", tmp_path / "state", "herdr", "ssh", WORKER, "bender", say=lambda m: None)
    r.api = api
    return r


def test_a_claim_without_a_profile_runs_text_only_and_asks_nobody(bare):
    cmd, _ = bare.helper_command({"family": "claude-code", "room_id": "room_abc"})
    assert cmd.startswith("claude -p") and "--tools ''" in cmd
    assert not hasattr(runner.Runner, "fetch_profile")
