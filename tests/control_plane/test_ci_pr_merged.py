from types import SimpleNamespace

from herdr_engineering.control_plane.project_links import mark_merged_pull_requests
from herdr_engineering.review_gate.models import PrKey
from herdr_engineering.review_gate.store import State


def _snap(repo, number, state):
    return SimpleNamespace(key=PrKey(repo, number), state=state)


class _Store:
    def __init__(self, *snaps):
        self._snaps = snaps

    def snapshots(self):
        return list(self._snaps)


def _view():
    return {"pipelines": {"o/r": [
        {"repository": "o/r", "ref": "refs/pull/7/head", "state": "failure"},
        {"repository": "o/r", "ref": "refs/pull/8/head", "state": "failure"},
        {"repository": "o/r", "ref": "refs/heads/main", "state": "success"},
        {"repository": "o/other", "ref": "refs/pull/7/head", "state": "failure"},
    ]}}


def test_only_runs_of_a_recorded_merged_pr_are_marked():
    store = _Store(_snap("o/r", 7, State.MERGED), _snap("o/r", 8, State.CI_PENDING))
    rows = mark_merged_pull_requests(_view(), store)["pipelines"]["o/r"]
    assert [bool(r.get("pr_merged")) for r in rows] == [True, False, False, False]


def test_no_gate_or_a_broken_gate_leaves_the_view_unchanged():
    assert "pr_merged" not in str(mark_merged_pull_requests(_view(), None))

    class Broken:
        def snapshots(self):
            raise RuntimeError("down")

    assert "pr_merged" not in str(mark_merged_pull_requests(_view(), Broken()))
