"""Pi adapter, against real (redacted) session lines laid out as Pi lays them out (sessions/<slug>/<ts>_<uuid>.jsonl)."""
import json
import shutil
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters.base import ExitPlan, Harness, SessionRef, TypedInput
from herdr_engineering.handoff_adapters.pi import (
    EDITOR_RULE,
    PiAdapter,
    session_id_from_agent_session,
    session_id_from_record_path,
)

FIXTURES = Path(__file__).parent / "fixtures" / "handoff_adapters" / "pi"
SESSION = "11111111-1111-7111-8111-111111111111"
OTHER = "22222222-2222-7222-8222-222222222222"
CWD = Path("/home/user/proj")
SLUG = "--home-user-proj--"  # Pi's directory naming, as observed on real session directories


def _install(home: Path, fixture: str, session: str = SESSION, slug: str = SLUG, models: bool = True) -> Path:
    directory = home / ".pi" / "agent" / "sessions" / slug
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"2026-11-17T18-08-38-549Z_{session}.jsonl"
    shutil.copy(FIXTURES / fixture, target)
    if models:
        shutil.copy(FIXTURES / "models.json", home / ".pi" / "agent" / "models.json")
    return target


def _ref(path: Path) -> SessionRef:
    return SessionRef(Harness.PI, "w1:p1", CWD, SESSION, path)


def _read(home: Path, fixture: str, **kw):
    return PiAdapter(home=home).read_context(_ref(_install(home, fixture, **kw)))


def test_read_context_uses_last_message_usage_total_tokens(tmp_path):
    reading = _read(tmp_path, "main.jsonl")
    assert reading is not None
    # real record: input 4826 + output 512 + cacheRead 14620 = totalTokens 19958 (that message is now in the context)
    assert reading.used_tokens == 19958
    assert (reading.window_tokens, reading.window_source) == (131_072, "recorded")  # models.json
    assert reading.source == "pi-session"
    assert reading.measured_at > 0


def test_none_before_first_assistant_message(tmp_path):
    assert _read(tmp_path, "no_assistant.jsonl") is None


def test_zero_or_missing_usage_is_skipped_not_read_as_zero(tmp_path):
    reading = _read(tmp_path, "zero_last.jsonl")  # a real aborted and a real errored zero-usage reply come last
    assert reading is not None and reading.used_tokens == 19958
    only = tmp_path / "only.jsonl"
    only.write_text(_install(tmp_path, "zero_last.jsonl").read_text().splitlines()[-1] + "\n")
    assert PiAdapter(home=tmp_path).read_context(_ref(only)) is None
    no_total = tmp_path / "no-total.jsonl"
    no_total.write_text('{"type":"message","message":{"role":"assistant","usage":{"input":5},"model":"m"}}\n')
    assert PiAdapter(home=tmp_path).read_context(_ref(no_total)) is None


def test_partial_last_line_is_skipped(tmp_path):
    reading = _read(tmp_path, "partial_last_line.jsonl")
    assert reading is not None and reading.used_tokens == 19958


def test_usage_before_a_compaction_is_stale_so_no_reading(tmp_path):
    assert _read(tmp_path, "compaction_last.jsonl") is None


@pytest.mark.parametrize("fixture,window,source", [
    ("model_change.jsonl", 262_144, "recorded"),            # qwen (131072) then deepseek_v4 (262144)
    ("model_change_reversed.jsonl", 131_072, "recorded"),
    ("recorded_then_table.jsonl", 200_000, "model_table"),  # recorded model, then claude-opus-4-8
    ("table_then_recorded.jsonl", 131_072, "recorded"),
])
def test_window_belongs_to_the_model_of_the_latest_usage_entry(tmp_path, fixture, window, source):
    reading = _read(tmp_path, fixture)
    assert reading is not None and reading.used_tokens == 40000
    assert (reading.window_tokens, reading.window_source) == (window, source)


def test_window_from_model_table_when_unrecorded(tmp_path):
    reading = _read(tmp_path, "table_then_recorded.jsonl", models=False)  # no models.json at all
    assert reading is not None
    assert (reading.window_tokens, reading.window_source) == (128_000, "fallback")  # qwen is in neither
    reading = _read(tmp_path, "recorded_then_table.jsonl", models=False)
    assert reading is not None
    assert (reading.window_tokens, reading.window_source) == (200_000, "model_table")


def test_unknown_model_uses_fallback_window(tmp_path):
    reading = _read(tmp_path, "unknown_model.jsonl")
    assert reading is not None and (reading.window_tokens, reading.window_source) == (128_000, "fallback")


def test_unreadable_models_json_falls_back_to_the_table(tmp_path):
    path = _install(tmp_path, "main.jsonl")
    (tmp_path / ".pi" / "agent" / "models.json").write_text("{not json")
    reading = PiAdapter(home=tmp_path).read_context(_ref(path))
    assert reading is not None and reading.window_source == "fallback"


def test_read_context_none_when_record_is_missing_or_unset(tmp_path):
    adapter = PiAdapter(home=tmp_path)
    assert adapter.read_context(_ref(tmp_path / "gone.jsonl")) is None
    assert adapter.read_context(SessionRef(Harness.PI, "w1:p1", CWD, SESSION, None)) is None


def test_session_directory_slug_follows_pi_naming(tmp_path):
    # Rule observed on real Pi session directories: "--" + cwd minus its leading separator, with / \ : as "-", + "--";
    # "." and "_" are kept.
    for cwd, slug in [
        (Path("/home/user/proj"), "--home-user-proj--"),
        (Path("/home/user/.cache/some_dir/pi-fixture"), "--home-user-.cache-some_dir-pi-fixture--"),
        (Path("C:\\work\\proj"), "--C--work-proj--"),
    ]:
        expected = _install(tmp_path, "main.jsonl", slug=slug)
        ref = PiAdapter(home=tmp_path).locate_session(SESSION, cwd, "w1:p1")
        assert ref == SessionRef(Harness.PI, "w1:p1", cwd, SESSION, expected)


def test_cwd_with_glob_characters_is_matched_literally(tmp_path):
    cwd = Path("/home/user/p[1]")
    expected = _install(tmp_path, "main.jsonl", slug="--home-user-p[1]--")
    _install(tmp_path, "main.jsonl", slug="--home-user-p1--")  # same uuid: an unescaped [1] would match here
    assert PiAdapter(home=tmp_path).locate_session(SESSION, cwd, "w1:p1").record_path == expected


def test_same_uuid_in_two_other_directories_is_ambiguous(tmp_path):
    _install(tmp_path, "main.jsonl", slug="--home-user-a--")
    _install(tmp_path, "main.jsonl", slug="--home-user-b--")
    assert PiAdapter(home=tmp_path).locate_session(SESSION, CWD, "w1:p1") is None


def test_the_cwd_directory_wins_over_another_directory_holding_the_same_uuid(tmp_path):
    _install(tmp_path, "main.jsonl", slug="--home-user-other--")
    wanted = _install(tmp_path, "main.jsonl")  # SLUG, the directory of CWD
    assert PiAdapter(home=tmp_path).locate_session(SESSION, CWD, "w1:p1").record_path == wanted


def test_locate_by_session_uuid(tmp_path):
    other = _install(tmp_path, "main.jsonl", OTHER)
    other.touch()  # the newest file is not the wanted one
    wanted = _install(tmp_path, "main.jsonl", SESSION)
    ref = PiAdapter(home=tmp_path).locate_session(SESSION, CWD, "w1:p1")
    assert ref == SessionRef(Harness.PI, "w1:p1", CWD, SESSION, wanted)


def test_locate_finds_the_session_when_the_pane_cwd_differs_from_the_session_directory(tmp_path):
    wanted = _install(tmp_path, "main.jsonl", slug="--home-user--")
    ref = PiAdapter(home=tmp_path).locate_session(SESSION, CWD, "w1:p1")
    assert ref is not None and ref.record_path == wanted


def test_locate_without_or_with_unknown_session_id_is_none(tmp_path):
    _install(tmp_path, "main.jsonl")
    adapter = PiAdapter(home=tmp_path)
    assert adapter.locate_session(None, CWD, "w1:p1") is None
    assert adapter.locate_session(OTHER, CWD, "w1:p1") is None


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "a\\b", "*", "a?b", "[a]"])
def test_session_id_with_path_parts_or_glob_characters_is_rejected(tmp_path, bad):
    _install(tmp_path, "main.jsonl")
    assert PiAdapter(home=tmp_path).locate_session(bad, CWD, "w1:p1") is None


def test_session_id_from_the_record_path_herdr_reports():
    # Herdr reports Pi sessions as kind "path", not "id"
    path = "/home/user/.pi/agent/sessions/--home-user-proj--/2026-01-02T03-04-05-678Z_33333333-3333-7333-8333-333333333333.jsonl"
    assert session_id_from_record_path(path) == "33333333-3333-7333-8333-333333333333"
    assert session_id_from_record_path("/x/notatimestamp.jsonl") is None
    assert session_id_from_record_path("") is None


def test_agent_session_kinds_id_and_path_are_accepted_and_others_rejected():
    uuid = "33333333-3333-7333-8333-333333333333"
    assert session_id_from_agent_session("id", uuid) == uuid
    assert session_id_from_agent_session("path", f"/h/sessions/--s--/2026-01-02T03-04-05-678Z_{uuid}.jsonl") == uuid
    assert session_id_from_agent_session("name", uuid) is None
    assert session_id_from_agent_session("", uuid) is None


def test_one_malformed_models_json_entry_does_not_hide_a_valid_match(tmp_path):
    path = _install(tmp_path, "main.jsonl")
    (tmp_path / ".pi" / "agent" / "models.json").write_text(
        '{"providers":{"metabolomics":{"models":[{"contextWindow":5},"junk",{"id":"qwen3.8-flash_next"},'
        '{"id":"qwen3.8-flash_next","contextWindow":65536}]}}}')
    reading = PiAdapter(home=tmp_path).read_context(_ref(path))
    assert reading is not None and (reading.window_tokens, reading.window_source) == (65_536, "recorded")


def test_locate_without_herdr_on_path_is_none(tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    _install(tmp_path, "main.jsonl")
    assert PiAdapter(home=tmp_path, env={"PATH": str(empty)}).locate("w1:p1", CWD) is None


def test_drain_compact_and_exit_inputs():
    adapter = PiAdapter()
    assert adapter.harness is Harness.PI
    assert adapter.drain_input("finish up") == TypedInput("finish up", ("Enter",))
    assert adapter.compact_input() == TypedInput("/compact", ("Enter",))
    assert adapter.exit_plan() == ExitPlan("/quit", ("Enter",), 15.0)
    with pytest.raises(ValueError):
        adapter.drain_input("x" * 901)
    with pytest.raises(ValueError):
        adapter.drain_input("é" * 451)  # 902 bytes in 451 characters


RULE = EDITOR_RULE * 80
# the idle bottom of a real Pi pane (herdr pane read): output, rule, empty editor line, rule, footer
IDLE_SCREEN = f" Both done.\n\n{RULE}\n\n{RULE}\n~ │ metabolomics/qwen3.8-flash_next\nMemory: idle\n"
WORKING_SCREEN = (" Current activity: Waiting for model · Esc to cancel\n"
                  f"{EDITOR_RULE * 2} ⠏ Working · retry in 42s {RULE}\n\n{RULE}\n~ │ metabolomics/qwen3.8-flash-vision\n")


@pytest.mark.parametrize("status", ["idle", "done"])
def test_ready_when_status_idle_or_done_and_editor_is_empty(status):
    assert PiAdapter().is_ready_for_input(IDLE_SCREEN, status)


def test_not_ready_while_working_or_with_typed_input_or_without_editor():
    adapter = PiAdapter()
    assert not adapter.is_ready_for_input(IDLE_SCREEN, "working")
    assert not adapter.is_ready_for_input(IDLE_SCREEN.replace(f"{RULE}\n\n{RULE}", f"{RULE}\nhalf typed\n{RULE}"), "idle")
    assert not adapter.is_ready_for_input(WORKING_SCREEN, "idle")
    assert not adapter.is_ready_for_input("just output\nmore output\n", "idle")
    assert not adapter.is_ready_for_input("", "done")


def test_ready_ignores_blank_padding_below_the_screen():
    assert PiAdapter().is_ready_for_input(IDLE_SCREEN + "\n" * 40, "idle")


def test_read_context_window_matches_recorded_field(tmp_path):
    reading = _read(tmp_path, "main.jsonl")  # provider metabolomics, model qwen3.8-flash_next
    entries = json.loads((FIXTURES / "models.json").read_text())["providers"]["metabolomics"]["models"]
    assert reading is not None
    assert reading.window_tokens == next(e["contextWindow"] for e in entries if e["id"] == "qwen3.8-flash_next")
    assert reading.window_source == "recorded"


# screens captured from a real pi pane (`herdr pane read --format ansi`), 2026-11-18, redacted
REAL_EMPTY = (FIXTURES / "screen_empty.ansi").read_text(encoding="utf-8")
REAL_TYPED = (FIXTURES / "screen_typed.ansi").read_text(encoding="utf-8")


@pytest.mark.parametrize("status", ["idle", "done"])
def test_real_screen_with_an_empty_prompt_is_ready(status):
    assert PiAdapter().is_ready_for_input(REAL_EMPTY, status)


def test_real_screen_with_typed_text_is_not_ready():
    assert not PiAdapter().is_ready_for_input(REAL_TYPED, "idle")


@pytest.mark.parametrize("status", ["working", "blocked", "unknown"])
def test_real_empty_screen_is_not_ready_while_the_harness_is_busy(status):
    assert not PiAdapter().is_ready_for_input(REAL_EMPTY, status)
