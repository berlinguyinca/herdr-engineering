"""Codex adapter, against real (redacted) rollout lines laid out as Codex lays them out (sessions/Y/M/D)."""
import json
import shutil
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters.base import ExitPlan, Harness, SessionRef, TypedInput
from herdr_engineering.handoff_adapters.codex import PROMPT_MARKER, CodexAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "handoff_adapters" / "codex"
SESSION = "01a1d6df-7c12-7671-bbf8-7c2d51d1a84b"
OTHER = "01a1c92e-0000-7000-8000-000000000002"


def _install(home: Path, fixture: str, session: str = SESSION, day: str = "2026/11/13") -> Path:
    directory = home / ".codex" / "sessions" / day
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"rollout-{day.replace('/', '-')}T13-45-32-{session}.jsonl"
    shutil.copy(FIXTURES / fixture, target)
    return target


def _ref(path: Path) -> SessionRef:
    return SessionRef(Harness.CODEX, "w1:p1", Path("/work"), SESSION, path)


def test_read_context_uses_last_token_count_event(tmp_path):
    reading = CodexAdapter(home=tmp_path).read_context(_ref(_install(tmp_path, "main.jsonl")))
    assert reading is not None
    assert reading.used_tokens == 28263  # last_token_usage of the second event, not the 56068 cumulative
    assert (reading.window_tokens, reading.window_source) == (828_400, "recorded")
    assert reading.source == "codex-rollout"
    assert reading.measured_at > 0


def test_context_is_last_token_usage_and_falls_after_compaction(tmp_path):
    # real numbers: last_token_usage 785180 before a compaction and 35545 after; the cumulative total never falls
    reading = CodexAdapter(home=tmp_path).read_context(_ref(_install(tmp_path, "compaction.jsonl")))
    assert reading is not None and reading.used_tokens == 35545


def test_a_compacted_record_after_the_last_usage_means_no_reading(tmp_path):
    # real layout: a top-level {"type": "compacted"} record; the usage before it is the context that was just discarded
    assert CodexAdapter(home=tmp_path).read_context(_ref(_install(tmp_path, "compaction_last.jsonl"))) is None


def test_none_when_no_token_count_event(tmp_path):
    assert CodexAdapter(home=tmp_path).read_context(_ref(_install(tmp_path, "no_token_count.jsonl"))) is None


def test_partial_last_line_is_skipped(tmp_path):
    reading = CodexAdapter(home=tmp_path).read_context(_ref(_install(tmp_path, "partial_last_line.jsonl")))
    assert reading is not None and reading.used_tokens == 28263


def test_token_count_with_null_info_is_skipped(tmp_path):
    reading = CodexAdapter(home=tmp_path).read_context(_ref(_install(tmp_path, "info_null_last.jsonl")))
    assert reading is not None and reading.used_tokens == 28263


def test_zero_usage_is_skipped_not_read_as_zero(tmp_path):
    reading = CodexAdapter(home=tmp_path).read_context(_ref(_install(tmp_path, "zero_last.jsonl")))
    assert reading is not None and reading.used_tokens == 28263


@pytest.mark.parametrize("fixture,window", [("window_change.jsonl", 272_000),
                                            ("window_change_reversed.jsonl", 828_400)])
def test_window_follows_the_latest_usage_event(tmp_path, fixture, window):
    reading = CodexAdapter(home=tmp_path).read_context(_ref(_install(tmp_path, fixture)))
    assert reading is not None
    assert (reading.window_tokens, reading.window_source) == (window, "recorded")


def test_window_from_model_table_when_the_event_records_none(tmp_path):
    reading = CodexAdapter(home=tmp_path).read_context(_ref(_install(tmp_path, "no_window.jsonl")))
    assert reading is not None
    assert (reading.window_tokens, reading.window_source) == (200_000, "model_table")  # model gpt-5-codex


@pytest.mark.parametrize("fixture,window,source", [("model_change.jsonl", 128_000, "fallback"),
                                                   ("model_change_reversed.jsonl", 200_000, "model_table")])
def test_unrecorded_window_follows_the_model_of_the_latest_usage_event(tmp_path, fixture, window, source):
    reading = CodexAdapter(home=tmp_path).read_context(_ref(_install(tmp_path, fixture)))
    assert reading is not None and reading.used_tokens == 40000
    assert (reading.window_tokens, reading.window_source) == (window, source)


def test_model_found_beyond_the_first_chunk(tmp_path):
    path = _install(tmp_path, "no_window.jsonl")
    filler = '{"type":"response_item","payload":{"type":"message","text":"redacted"}}\n'
    lines = path.read_text().splitlines(keepends=True)
    path.write_text(lines[0] + filler * 20_000 + "".join(lines[1:]))  # > 1 MiB between model and usage
    reading = CodexAdapter(home=tmp_path).read_context(_ref(path))
    assert reading is not None and (reading.window_tokens, reading.window_source) == (200_000, "model_table")


def test_read_context_none_when_record_is_missing_or_unset(tmp_path):
    adapter = CodexAdapter(home=tmp_path)
    assert adapter.read_context(_ref(tmp_path / "gone.jsonl")) is None
    assert adapter.read_context(SessionRef(Harness.CODEX, "w1:p1", Path("/work"), SESSION, None)) is None


def test_locate_matches_rollout_by_session_id_in_filename(tmp_path):
    other = _install(tmp_path, "main.jsonl", OTHER, "2026/11/14")
    other.touch()  # the newest file is not the wanted one
    wanted = _install(tmp_path, "main.jsonl", SESSION, "2026/11/13")
    ref = CodexAdapter(home=tmp_path).locate_session(SESSION, Path("/work"), "w1:p1")
    assert ref == SessionRef(Harness.CODEX, "w1:p1", Path("/work"), SESSION, wanted)


def test_locate_without_or_with_unknown_session_id_is_none(tmp_path):
    _install(tmp_path, "main.jsonl")
    adapter = CodexAdapter(home=tmp_path)
    assert adapter.locate_session(None, Path("/work"), "w1:p1") is None
    assert adapter.locate_session(OTHER, Path("/work"), "w1:p1") is None


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "a\\b", "*", "a?b", "[a]"])
def test_session_id_with_path_parts_or_glob_characters_is_rejected(tmp_path, bad):
    _install(tmp_path, "main.jsonl")
    assert CodexAdapter(home=tmp_path).locate_session(bad, Path("/work"), "w1:p1") is None


def test_locate_without_herdr_on_path_is_none(tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    _install(tmp_path, "main.jsonl")
    assert CodexAdapter(home=tmp_path, env={"PATH": str(empty)}).locate("w1:p1", Path("/work")) is None


def test_drain_compact_and_exit_inputs():
    adapter = CodexAdapter()
    assert adapter.harness is Harness.CODEX
    assert adapter.drain_input("finish up") == TypedInput("finish up", ("Enter",))
    assert adapter.compact_input() == TypedInput("/compact", ("Enter",))
    assert adapter.exit_plan() == ExitPlan("/exit", ("Enter",), 15.0)
    with pytest.raises(ValueError):
        adapter.drain_input("x" * 901)


IDLE_SCREEN = f"previous output\n\n{PROMPT_MARKER} \n\n  gpt-5-codex low · ~/work\n"


@pytest.mark.parametrize("status", ["idle", "done"])
def test_ready_when_status_idle_or_done_and_prompt_is_empty(status):
    assert CodexAdapter().is_ready_for_input(IDLE_SCREEN, status)


def test_not_ready_while_working_or_with_typed_input_or_without_prompt():
    adapter = CodexAdapter()
    assert not adapter.is_ready_for_input(IDLE_SCREEN, "working")
    assert not adapter.is_ready_for_input(f"out\n{PROMPT_MARKER} half typed\n\n  status\n", "idle")
    assert not adapter.is_ready_for_input("just output\nmore output\n", "idle")
    assert not adapter.is_ready_for_input("", "done")


def test_ready_ignores_blank_padding_below_the_screen():
    assert CodexAdapter().is_ready_for_input(IDLE_SCREEN + "\n" * 40, "idle")


def test_read_context_window_matches_recorded_field(tmp_path):
    path = _install(tmp_path, "main.jsonl")
    events = [json.loads(line)["payload"]["info"] for line in path.read_text().splitlines()
              if '"token_count"' in line]
    reading = CodexAdapter(home=tmp_path).read_context(_ref(path))
    assert reading is not None
    assert reading.window_tokens == events[-1]["model_context_window"]
    assert reading.window_source == "recorded"


# screens captured from a real codex pane (`herdr pane read --format ansi`), 2026-11-18, redacted
REAL_EMPTY = (FIXTURES / "screen_empty.ansi").read_text(encoding="utf-8")
REAL_TYPED = (FIXTURES / "screen_typed.ansi").read_text(encoding="utf-8")


@pytest.mark.parametrize("status", ["idle", "done"])
def test_real_screen_with_an_empty_prompt_is_ready(status):
    assert CodexAdapter().is_ready_for_input(REAL_EMPTY, status)


def test_real_screen_with_typed_text_is_not_ready():
    assert not CodexAdapter().is_ready_for_input(REAL_TYPED, "idle")


@pytest.mark.parametrize("status", ["working", "blocked", "unknown"])
def test_real_empty_screen_is_not_ready_while_the_harness_is_busy(status):
    assert not CodexAdapter().is_ready_for_input(REAL_EMPTY, status)
