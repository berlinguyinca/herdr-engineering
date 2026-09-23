"""Unified test explorer tests (spec 0120)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest

from herdr_engineering.tests import JunitAdapter, RunContext, TestExplorer


def _ctx(**kw):
    return RunContext(repository="r", worktree_id="wt", machine_id="m",
                      revision="rev", **kw)


def test_adapters_registered():
    assert TestExplorer().adapters() == ["cargo", "go", "junit", "pytest", "scala"]


def test_junit_adapter_parses_xml(tmp_path):
    (tmp_path / "target").mkdir()
    sr = tmp_path / "target" / "surefire-reports"
    sr.mkdir()
    (sr / "TEST-MyTest.xml").write_text(
        '<testsuite name="com.x.MyTest"><testcase name="a" time="0.01"/>'
        '<testcase name="b" time="0.02"><failure message="boom"/></testcase>'
        '</testsuite>')
    ctx = _ctx(workdir=str(tmp_path))
    events = JunitAdapter().run(ctx)
    finished = [e for e in events if e.type == "test.finished"]
    assert len(finished) == 2
    statuses = {e.test_id: e.result_status for e in finished}
    assert statuses["a"] == "passed"
    assert statuses["b"] == "failed"


def test_build_tree(tmp_path):
    # Run the pytest adapter against a tiny isolated project so the test is
    # fast and deterministic (never recursing over this repository).
    (tmp_path / "test_demo.py").write_text(
        "def test_ok():\n    assert True\n\n"
        "def test_bad():\n    assert False\n")
    ctx = _ctx(workdir=str(tmp_path))
    explorer = TestExplorer()
    events = explorer.run("pytest", ctx)
    tree = TestExplorer.build_tree(events)
    assert tree["id"] == "root"
    # both tests surfaced; one passed, one failed
    finished = [e for e in events if e.type == "test.finished"]
    assert len(finished) == 2
    assert sorted(e.test_id for e in finished) == [
        "test_demo.py::test_bad", "test_demo.py::test_ok"]
    assert {e.result_status for e in finished} == {"passed", "failed"}


def test_unknown_adapter_rejected():
    with pytest.raises(Exception):
        TestExplorer().run("nope", _ctx())


def test_cancel_returns_no_tests():
    ctx = _ctx()
    events = TestExplorer().run("pytest", ctx, cancel=True)
    assert all(e.type in ("run.started", "run.finished") for e in events)
