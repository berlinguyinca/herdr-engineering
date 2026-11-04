import base64
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("wp_log", Path(__file__).resolve().parents[1] / "scripts" / "ci" / "wp_log.py")
wp_log = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp_log)


def entry(line, text):
    return {"line": line, "data": base64.b64encode(text.encode()).decode() if text is not None else None}


def test_decode_orders_lines_and_skips_empty_entries():
    lines = wp_log.decode_log([entry(2, "c\n"), entry(0, "a"), entry(1, None), entry(3, "d\ne")])
    assert lines == ["a", "c", "d", "e"]


def test_failure_blocks_find_the_failing_test_not_background_tracebacks():
    lines = ["ok 1 - fine", "Traceback (most recent call last):", "  BrokenPipeError", "ok 2 - fine", "not ok 3 - broken", "  error: boom", "ok 4"]
    blocks = wp_log.failure_blocks(lines, before=0, after=1)
    assert blocks == [["not ok 3 - broken", "  error: boom"]]


def test_overlapping_blocks_merge():
    lines = ["not ok 1", "x", "not ok 2", "y", "z"]
    assert len(wp_log.failure_blocks(lines, before=0, after=2)) == 1
