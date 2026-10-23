"""The runner must never report green for a suite that ran nothing, ran short, skipped, or is unlisted
(the 'muted exit code hid a green build that ran nothing' failure mode)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("run_acceptance", Path(__file__).resolve().parents[2] / "scripts" / "run_acceptance.py")
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)

MANIFEST = {"herdr": {"tests/e2e/test_a.py": {"scenario": "s1", "min_tests": 3, "max_xfail": 1}},
            "pi": {"test/e2e/b.e2e.test.ts": {"scenario": "s1", "min_tests": 2}}}
GOOD_H = {"exit": 0, "files": {"test_a.py": {"tests": 3, "passed": 2, "failed": 0, "xfailed": 1, "skipped": 0, "gaps": []}}}
GOOD_P = {"test/e2e/b.e2e.test.ts": {"exit": 0, "tests": 2, "passed": 2, "failed": 0, "skipped": 0, "todo": 0, "cancelled": 0, "tail": "", "gaps": []}}


def judge(h=GOOD_H, p=GOOD_P, disk=("test/e2e/b.e2e.test.ts",), tags=None):
    return ra.judge(MANIFEST, h, p, tags, list(disk))


def test_a_complete_run_is_green():
    assert judge() == []


def test_zero_tests_short_runs_skips_failures_and_unlisted_files_all_fail_the_run():
    import copy
    h = copy.deepcopy(GOOD_H); h["files"]["test_a.py"]["tests"] = 0
    assert any("ZERO" in f for f in judge(h=h))
    h = copy.deepcopy(GOOD_H); h["files"]["test_a.py"]["tests"] = 2
    assert any("expected at least 3" in f for f in judge(h=h))
    h = copy.deepcopy(GOOD_H); h["files"]["test_a.py"]["skipped"] = 1
    assert any("skipped" in f for f in judge(h=h))
    h = copy.deepcopy(GOOD_H); h["files"]["test_a.py"]["xfailed"] = 2
    assert any("xfail" in f for f in judge(h=h))
    h = copy.deepcopy(GOOD_H); h["exit"] = 1
    assert any("exit 1" in f for f in judge(h=h))
    h = {"exit": 0, "files": {}}
    assert any("ZERO" in f for f in judge(h=h))
    p = copy.deepcopy(GOOD_P); p["test/e2e/b.e2e.test.ts"]["tests"] = 0
    assert any("ZERO" in f for f in judge(p=p))
    p = copy.deepcopy(GOOD_P); p["test/e2e/b.e2e.test.ts"]["todo"] = 1
    assert any("todo" in f for f in judge(p=p))
    assert any("not in manifest" in f for f in judge(disk=("test/e2e/b.e2e.test.ts", "test/e2e/new.e2e.test.ts")))
    assert any("file missing" in f for f in judge(p={"test/e2e/b.e2e.test.ts": None}))


def test_a_pi_file_may_not_name_more_tests_gap_than_the_manifest_allows():
    import copy
    p = copy.deepcopy(GOOD_P)
    p["test/e2e/b.e2e.test.ts"]["gaps"] = ["GAP: x"]
    assert any("named GAP" in f for f in judge(p=p))                       # max_gap defaults to 0
    m = copy.deepcopy(MANIFEST); m["pi"]["test/e2e/b.e2e.test.ts"]["max_gap"] = 1
    assert ra.judge(m, GOOD_H, p, None, ["test/e2e/b.e2e.test.ts"]) == []   # an allowed, listed gap is fine


def test_xfail_must_be_the_strict_marker():
    ok = "import pytest\n@pytest.mark.xfail(strict=True, reason='GAP: x')\ndef test_a():\n    assert 0\n"
    assert ra.xfail_violations({"t.py": ok}) == []
    for bad in ("@pytest.mark.xfail(reason='x')\ndef test_a(): pass\n",
                "@pytest.mark.xfail(strict=False)\ndef test_a(): pass\n",
                "def test_a():\n    pytest.xfail('later')\n"):
        assert ra.xfail_violations({"t.py": "import pytest\n" + bad}), bad
