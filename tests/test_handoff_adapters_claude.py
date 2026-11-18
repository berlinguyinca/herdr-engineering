"""Claude Code adapter, against real (redacted) transcript files laid out as Claude Code lays them out."""
import json
import re
import shutil
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters.base import ExitPlan, Harness, SessionRef, TypedInput
from herdr_engineering.handoff_adapters.claude import PROMPT_MARKER, ClaudeAdapter
from herdr_engineering.handoff_context_hook import context_tokens

FIXTURES = Path(__file__).parent / "fixtures" / "handoff_adapters" / "claude"
SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def _install(home: Path, cwd: Path, fixture: str, session: str = SESSION) -> Path:
    directory = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{session}.jsonl"
    shutil.copy(FIXTURES / fixture, target)
    return target


def _ref(path: Path, cwd: Path) -> SessionRef:
    return SessionRef(Harness.CLAUDE, "w1:p1", cwd, SESSION, path)


def test_read_context_sums_input_cache_read_and_cache_creation_of_last_assistant_usage(tmp_path):
    cwd = tmp_path / "work.tree"
    path = _install(tmp_path, cwd, "main_chain.jsonl")
    reading = ClaudeAdapter(home=tmp_path).read_context(_ref(path, cwd))
    assert reading is not None
    assert reading.used_tokens == 2 + 5000 + 700
    assert (reading.window_tokens, reading.window_source) == (200_000, "model_table")
    assert reading.source == "claude-transcript"
    assert reading.measured_at > 0


def test_read_context_ignores_subagent_and_synthetic_entries(tmp_path):
    cwd = tmp_path / "work"
    path = _install(tmp_path, cwd, "main_chain.jsonl")
    reading = ClaudeAdapter(home=tmp_path).read_context(_ref(path, cwd))
    assert reading is not None and reading.used_tokens == 5702  # not the sidechain 1_000_017, not synthetic 0


def test_read_context_none_before_first_assistant_turn(tmp_path):
    cwd = tmp_path / "work"
    path = _install(tmp_path, cwd, "no_assistant.jsonl")
    assert ClaudeAdapter(home=tmp_path).read_context(_ref(path, cwd)) is None


def test_partial_last_line_is_skipped(tmp_path):
    cwd = tmp_path / "work"
    path = _install(tmp_path, cwd, "partial_last_line.jsonl")
    reading = ClaudeAdapter(home=tmp_path).read_context(_ref(path, cwd))
    assert reading is not None and reading.used_tokens == 1203 == context_tokens(path)  # same sum as the hook


@pytest.mark.parametrize("fixture,window", [("model_change.jsonl", 200_000),
                                            ("model_change_reversed.jsonl", 1_000_000)])
def test_window_follows_the_model_of_the_latest_usage_entry(tmp_path, fixture, window):
    cwd = tmp_path / "work"
    path = _install(tmp_path, cwd, fixture)
    reading = ClaudeAdapter(home=tmp_path).read_context(_ref(path, cwd))
    assert reading is not None
    assert (reading.window_tokens, reading.window_source) == (window, "model_table")
    assert reading.used_tokens == 6104


def test_usage_without_token_keys_is_skipped_not_read_as_zero(tmp_path):
    cwd = tmp_path / "work"
    path = _install(tmp_path, cwd, "empty_usage_last.jsonl")
    reading = ClaudeAdapter(home=tmp_path).read_context(_ref(path, cwd))
    assert reading is not None and reading.used_tokens == 1203
    only = tmp_path / "only.jsonl"
    only.write_text(path.read_text().splitlines()[-1] + "\n")
    assert ClaudeAdapter(home=tmp_path).read_context(_ref(only, cwd)) is None


def test_read_context_none_when_record_is_missing(tmp_path):
    cwd = tmp_path / "work"
    assert ClaudeAdapter(home=tmp_path).read_context(_ref(tmp_path / "gone.jsonl", cwd)) is None


def test_transcript_directory_replaces_every_non_alphanumeric_character(tmp_path):
    # Real Claude Code names: /private/var/folders/m_/hg7g..tg_b_l04..  ->  -private-var-folders-m--hg7g..tg-b-l04..
    cwd = Path("/private/var/m_/a_b c.d/é")
    expected = tmp_path / ".claude" / "projects" / "-private-var-m--a-b-c-d--"
    expected.mkdir(parents=True)
    (expected / f"{SESSION}.jsonl").write_text("")
    ref = ClaudeAdapter(home=tmp_path).locate_session(SESSION, cwd, "w1:p1")
    assert ref is not None and ref.record_path == expected / f"{SESSION}.jsonl"


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "a\\b"])
def test_session_id_with_path_parts_is_rejected(tmp_path, bad):
    cwd = tmp_path / "work"
    _install(tmp_path, cwd, "main_chain.jsonl")
    assert ClaudeAdapter(home=tmp_path).locate_session(bad, cwd, "w1:p1") is None


def test_locate_without_herdr_on_path_falls_back_to_the_only_transcript(tmp_path):
    cwd = tmp_path / "work"
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    adapter = ClaudeAdapter(home=tmp_path, env={"PATH": str(empty)})
    assert adapter.locate("w1:p1", cwd) is None
    only = _install(tmp_path, cwd, "main_chain.jsonl")
    assert adapter.locate("w1:p1", cwd).record_path == only
    _install(tmp_path, cwd, "model_change.jsonl", OTHER)
    assert adapter.locate("w1:p1", cwd) is None


def test_unknown_model_uses_fallback_window_not_none(tmp_path):
    cwd = tmp_path / "work"
    path = _install(tmp_path, cwd, "main_chain.jsonl")
    path.write_text(path.read_text().replace("claude-opus-4-8", "claude-future-9"))
    reading = ClaudeAdapter(home=tmp_path).read_context(_ref(path, cwd))
    assert reading is not None and (reading.window_tokens, reading.window_source) == (128_000, "fallback")


def test_locate_picks_transcript_by_session_id_not_newest(tmp_path):
    cwd = tmp_path / "work.tree"
    wanted = _install(tmp_path, cwd, "main_chain.jsonl", SESSION)
    newest = _install(tmp_path, cwd, "model_change.jsonl", OTHER)
    newest.touch()
    ref = ClaudeAdapter(home=tmp_path).locate_session(SESSION, cwd, "w1:p1")
    assert ref == SessionRef(Harness.CLAUDE, "w1:p1", cwd, SESSION, wanted)


def test_locate_without_session_id_needs_exactly_one_transcript(tmp_path):
    cwd = tmp_path / "work"
    adapter = ClaudeAdapter(home=tmp_path)
    assert adapter.locate_session(None, cwd, "w1:p1") is None
    only = _install(tmp_path, cwd, "main_chain.jsonl", SESSION)
    ref = adapter.locate_session(None, cwd, "w1:p1")
    assert ref is not None and ref.record_path == only and ref.session_id == SESSION
    _install(tmp_path, cwd, "model_change.jsonl", OTHER)
    assert adapter.locate_session(None, cwd, "w1:p1") is None


def test_locate_unknown_session_id_is_none(tmp_path):
    cwd = tmp_path / "work"
    _install(tmp_path, cwd, "main_chain.jsonl", SESSION)
    assert ClaudeAdapter(home=tmp_path).locate_session(OTHER, cwd, "w1:p1") is None


def test_drain_compact_and_exit_inputs():
    adapter = ClaudeAdapter()
    assert adapter.harness is Harness.CLAUDE
    assert adapter.drain_input("finish up") == TypedInput("finish up", ("Enter",))
    assert adapter.compact_input() == TypedInput("/compact", ("Enter",))
    assert adapter.exit_plan() == ExitPlan("/exit", ("Enter",), 15.0)
    with pytest.raises(ValueError):
        adapter.drain_input("x" * 901)


RULE = "─" * 120
IDLE_SCREEN = f"previous output\n{RULE}\n{PROMPT_MARKER}\n{RULE}\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"


@pytest.mark.parametrize("status", ["idle", "done"])
def test_ready_when_status_idle_or_done_and_prompt_is_empty(status):
    assert ClaudeAdapter().is_ready_for_input(IDLE_SCREEN, status)


def test_not_ready_while_working_or_with_typed_input_or_without_prompt():
    adapter = ClaudeAdapter()
    assert not adapter.is_ready_for_input(IDLE_SCREEN, "working")
    typed = f"out\n{RULE}\n{PROMPT_MARKER} half typed\n{RULE}\n  status\n"
    assert not adapter.is_ready_for_input(typed, "idle")
    assert not adapter.is_ready_for_input("just output\nmore output\n", "idle")
    assert not adapter.is_ready_for_input("", "done")


def test_ready_ignores_blank_padding_below_the_screen():
    assert ClaudeAdapter().is_ready_for_input(IDLE_SCREEN + "\n" * 40, "idle")


def _install_catalog(home: Path, *names: str) -> None:
    directory = home / ".claude" / "cache" / "model-catalog"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(FIXTURES / name, directory / f"org-{name.removesuffix('.json')}-cc.json")


def _catalog_limit(name: str, model: str) -> int:
    models = json.loads((FIXTURES / name).read_text())["catalog"]["config"]["models"]
    return next(m["runtime"]["max_input_tokens"] for m in models if m["id"] == model)


def _write_catalog(home: Path, name: str, models: dict[str, int], fetched: int | None = 1_795_040_000_000) -> Path:
    directory = home / ".claude" / "cache" / "model-catalog"
    directory.mkdir(parents=True, exist_ok=True)
    body: dict = {"version": 2, "catalog": {"config": {"models": [
        {"id": m, "runtime": {"max_input_tokens": w}} for m, w in models.items()]}}}
    if fetched is not None:
        body["fetchedAt"] = fetched
    path = directory / f"{name}-cc.json"
    path.write_text(json.dumps(body))
    return path


def _with_used(path: Path, used: int) -> None:
    """Rewrite the latest main-chain usage so that it sums to `used` (all of it as cache reads)."""
    path.write_text(path.read_text().replace('"cache_read_input_tokens": 5000', f'"cache_read_input_tokens": {used - 702}'))


def _read(tmp_path: Path, used: int | None = None, model: str | None = None):
    cwd = tmp_path / "work"
    path = _install(tmp_path, cwd, "main_chain.jsonl")  # model claude-opus-4-8, which the table says is 200000
    if used is not None:
        _with_used(path, used)
    if model is not None:
        path.write_text(path.read_text().replace("claude-opus-4-8", model))
    return ClaudeAdapter(home=tmp_path).read_context(_ref(path, cwd))


def test_read_context_window_matches_recorded_field(tmp_path):
    _install_catalog(tmp_path, "model-catalog.json")
    reading = _read(tmp_path, model="claude-mythos-5-1")  # only the catalogue lists it
    assert reading is not None
    assert reading.window_tokens == _catalog_limit("model-catalog.json", "claude-mythos-5-1") == 1_000_000
    assert reading.window_source == "recorded"


def test_newest_catalogue_listing_the_model_wins(tmp_path):
    _install_catalog(tmp_path, "model-catalog-older.json", "model-catalog.json")
    reading = _read(tmp_path, model="claude-mythos-5-1")
    assert (reading.window_tokens, reading.window_source) == (1_000_000, "recorded")


def test_model_the_catalogue_does_not_list_uses_the_table(tmp_path):
    _install_catalog(tmp_path, "model-catalog.json")
    reading = _read(tmp_path, model="claude-opus-4-5")
    assert (reading.window_tokens, reading.window_source) == (200_000, "model_table")


def test_unreadable_catalogue_falls_back_to_the_table(tmp_path):
    _write_catalog(tmp_path, "org-y", {"claude-opus-4-8": 0})  # a valid fetchedAt, so the non-positive check is what rejects it
    (tmp_path / ".claude" / "cache" / "model-catalog" / "org-x-cc.json").write_text("{not json")
    reading = _read(tmp_path)
    assert (reading.window_tokens, reading.window_source) == (200_000, "model_table")


def test_catalogue_without_fetched_at_is_ignored(tmp_path):
    _write_catalog(tmp_path, "org-y", {"claude-opus-4-8": 150_000}, fetched=None)
    reading = _read(tmp_path)
    assert (reading.window_tokens, reading.window_source) == (200_000, "model_table")


def test_a_half_written_catalogue_is_skipped_and_a_valid_one_still_used(tmp_path):
    good = _write_catalog(tmp_path, "org-good", {"claude-mythos-5-1": 1_000_000})
    text = good.read_text()
    (good.parent / "org-half-cc.json").write_text(text[: len(text) // 2])  # truncated JSON
    reading = _read(tmp_path, model="claude-mythos-5-1")
    assert (reading.window_tokens, reading.window_source) == (1_000_000, "recorded")


@pytest.mark.parametrize("listed,seen", [("claude-future-7", "claude-future-7-20260101"),
                                         ("claude-future-7-20260101", "claude-future-7"),
                                         ("claude-future-7-20260101", "claude-future-7-20260202")])
def test_dated_and_undated_ids_match_each_other_in_the_catalogue(tmp_path, listed, seen):
    _write_catalog(tmp_path, "org-x", {listed: 300_000})
    reading = _read(tmp_path, model=seen)
    assert (reading.window_tokens, reading.window_source) == (300_000, "recorded")


def test_the_real_dated_catalogue_id_matches_a_dated_transcript_id(tmp_path):
    # the real catalogue lists claude-haiku-4-5-20251001 (dated); the table says 200000 for claude-haiku-4-5
    _install_catalog(tmp_path, "model-catalog.json")
    reading = _read(tmp_path, model="claude-haiku-4-5-20251001")
    assert (reading.window_tokens, reading.window_source) == (200_000, "recorded")


def test_conflict_with_usage_below_the_smaller_window_uses_the_smaller_table_window(tmp_path):
    _write_catalog(tmp_path, "org-x", {"claude-opus-4-8": 1_000_000})  # the table says 200000 for the same id
    reading = _read(tmp_path, used=150_000)
    assert (reading.window_tokens, reading.window_source) == (200_000, "model_table")


def test_conflict_with_usage_above_the_smaller_window_uses_the_larger_recorded_window(tmp_path):
    _write_catalog(tmp_path, "org-x", {"claude-opus-4-8": 1_000_000})
    reading = _read(tmp_path, used=250_000)  # more than 200000 proves the table's window wrong for this session
    assert (reading.window_tokens, reading.window_source) == (1_000_000, "recorded")


def test_conflict_with_a_smaller_catalogue_window_uses_it_while_usage_fits(tmp_path):
    _write_catalog(tmp_path, "org-x", {"claude-opus-4-8": 150_000})
    reading = _read(tmp_path, used=100_000)
    assert (reading.window_tokens, reading.window_source) == (150_000, "recorded")


def test_conflict_with_a_smaller_catalogue_window_uses_the_larger_table_window_once_usage_exceeds_it(tmp_path):
    _write_catalog(tmp_path, "org-x", {"claude-opus-4-8": 150_000})
    reading = _read(tmp_path, used=170_000)
    assert (reading.window_tokens, reading.window_source) == (200_000, "model_table")


def test_catalogue_and_table_that_agree_are_recorded(tmp_path):
    _write_catalog(tmp_path, "org-x", {"claude-opus-4-8": 200_000})
    reading = _read(tmp_path)
    assert (reading.window_tokens, reading.window_source) == (200_000, "recorded")


def test_table_only_and_catalogue_only_use_what_exists(tmp_path):
    only_table = _read(tmp_path)
    assert (only_table.window_tokens, only_table.window_source) == (200_000, "model_table")
    _write_catalog(tmp_path / "c", "org-x", {"claude-future-9": 400_000})
    only_catalogue = _read(tmp_path / "c", model="claude-future-9")
    assert (only_catalogue.window_tokens, only_catalogue.window_source) == (400_000, "recorded")


def test_a_compact_boundary_after_the_last_usage_means_no_reading(tmp_path):
    cwd = tmp_path / "work"
    path = _install(tmp_path, cwd, "compaction_last.jsonl")
    assert ClaudeAdapter(home=tmp_path).read_context(_ref(path, cwd)) is None


def test_a_reply_after_the_compact_boundary_is_read(tmp_path):
    cwd = tmp_path / "work"
    path = _install(tmp_path, cwd, "compaction_then_reply.jsonl")
    reading = ClaudeAdapter(home=tmp_path).read_context(_ref(path, cwd))
    assert reading is not None and reading.used_tokens == 4 + 900 + 100


# screens captured from a real claude pane (`herdr pane read --format ansi`), 2026-11-18, redacted
REAL_EMPTY = (FIXTURES / "screen_empty.ansi").read_text(encoding="utf-8")
REAL_TYPED = (FIXTURES / "screen_typed.ansi").read_text(encoding="utf-8")


@pytest.mark.parametrize("status", ["idle", "done"])
def test_real_screen_with_an_empty_prompt_is_ready(status):
    assert ClaudeAdapter().is_ready_for_input(REAL_EMPTY, status)


def test_real_screen_with_typed_text_is_not_ready():
    assert not ClaudeAdapter().is_ready_for_input(REAL_TYPED, "idle")


@pytest.mark.parametrize("status", ["working", "blocked", "unknown"])
def test_real_empty_screen_is_not_ready_while_the_harness_is_busy(status):
    assert not ClaudeAdapter().is_ready_for_input(REAL_EMPTY, status)
