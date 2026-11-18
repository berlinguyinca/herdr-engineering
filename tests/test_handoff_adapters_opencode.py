"""OpenCode adapter, against a real (redacted) subset of OpenCode's SQLite database.

The fixture database is built by scripts/build_opencode_fixture.py from one real session: one session row and
its message rows (ids synthetic, every text value redacted; roles, timestamps, model ids and token counts kept).
Variants are made by writing more rows into a copy with a real sqlite connection.
"""
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters.base import ExitPlan, Harness, SessionRef, TypedInput
from herdr_engineering.handoff_adapters.opencode import PROMPT_PLACEHOLDER, OpenCodeAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "handoff_adapters" / "opencode"
SESSION = "ses_fixture0001"
OTHER = "ses_fixture0002"
CWD = Path("/home/user/proj")
_TOKENS = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}


def _db_path(home: Path) -> Path:
    return home / ".local" / "share" / "opencode" / "opencode.db"


def _build(db: Path) -> None:
    """Build a database from the committed SQL dump (a .db file is gitignored, so the fixture is text)."""
    con = sqlite3.connect(db)
    try:
        con.executescript((FIXTURES / "opencode.sql").read_text(encoding="utf-8"))
    finally:
        con.close()


def _install(home: Path, config: bool = True, catalogue: bool = True) -> Path:
    db = _db_path(home)
    db.parent.mkdir(parents=True, exist_ok=True)
    _build(db)
    if config:
        (home / ".config" / "opencode").mkdir(parents=True)
        shutil.copy(FIXTURES / "opencode.json", home / ".config" / "opencode" / "opencode.json")
    if catalogue:
        (home / ".cache" / "opencode").mkdir(parents=True)
        shutil.copy(FIXTURES / "models.json", home / ".cache" / "opencode" / "models.json")
    return db


def _message(db: Path, number: int, data: dict, session: str = SESSION) -> None:
    """Append a message row newer than everything in the fixture."""
    con = sqlite3.connect(db)
    try:
        con.execute("insert into message (id, session_id, time_created, time_updated, data) values (?, ?, ?, ?, ?)",
                    (f"msg_extra{number:04d}", session, 2_000_000_000_000 + number, 2_000_000_000_000 + number, json.dumps(data)))
        con.commit()
    finally:
        con.close()


def _reply(model: str, total: int | None, provider: str = "ollama", **extra) -> dict:
    tokens = dict(_TOKENS) if total is None else {"total": total, **_TOKENS}
    return {"role": "assistant", "providerID": provider, "modelID": model, "tokens": tokens, **extra}


def _session(db: Path, session: str, directory: str, parent: str | None = None, archived: int | None = None) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute("insert into session (id, project_id, parent_id, slug, directory, title, version, time_created, "
                    "time_updated, time_archived) values (?, 'redacted', ?, 'redacted', ?, 'redacted', 'redacted', 1, 1, ?)",
                    (session, parent, directory, archived))
        con.commit()
    finally:
        con.close()


def _ref(db: Path | None, session: str | None = SESSION) -> SessionRef:
    return SessionRef(Harness.OPENCODE, "w1:p1", CWD, session, db)


def _read(home: Path, **kw):
    return OpenCodeAdapter(home=home, env={}).read_context(_ref(_install(home, **kw)))


def test_read_context_from_latest_assistant_message_tokens(tmp_path):
    reading = _read(tmp_path)
    assert reading is not None
    # real row: input 54385 + output 157 = tokens.total 54542 (that reply is now in the context)
    assert reading.used_tokens == 54542
    assert (reading.window_tokens, reading.window_source) == (131_072, "recorded")  # opencode.json, ollama/gemma4:26b
    assert reading.source == "opencode-db"
    assert reading.measured_at > 0


def test_total_is_the_sum_of_every_token_field_when_missing(tmp_path):
    db = _install(tmp_path)
    tokens = {"input": 100, "output": 20, "reasoning": 5, "cache": {"read": 300, "write": 7}}
    _message(db, 1, {"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b", "tokens": tokens})
    assert OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db)).used_tokens == 432


def test_zero_or_missing_usage_is_skipped_not_read_as_zero(tmp_path):
    db = _install(tmp_path)
    _message(db, 1, _reply("gemma4:26b", None))  # a real zero-usage reply comes last
    _message(db, 2, {"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b"})
    _message(db, 3, {"role": "user"})
    reading = OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db))
    assert reading is not None and reading.used_tokens == 54542
    only = tmp_path / "only.db"
    _build(only)
    con = sqlite3.connect(only)
    con.execute("delete from message where json_extract(data, '$.role') = 'assistant' and "
                "coalesce(json_extract(data, '$.tokens.total'), 0) > 0")
    con.commit()
    con.close()
    assert OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(only)) is None


def test_none_before_first_assistant_message(tmp_path):
    db = _install(tmp_path)
    con = sqlite3.connect(db)
    con.execute("delete from message where json_extract(data, '$.role') = 'assistant'")
    con.commit()
    con.close()
    assert OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db)) is None


def test_usage_before_a_compaction_is_stale_so_no_reading(tmp_path):
    db = _install(tmp_path)
    # a summary reply: OpenCode marks it summary + mode compaction; the context just shrank to the summary
    _message(db, 1, _reply("gemma4:26b", 2520, summary=True, mode="compaction"))
    _message(db, 2, _reply("gemma4:26b", None))  # the zero-usage reply that follows a real compaction
    assert OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db)) is None
    _message(db, 3, _reply("gemma4:26b", 3100))  # the first reply after it measures again
    assert OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db)).used_tokens == 3100


def test_only_the_requested_session_is_read(tmp_path):
    db = _install(tmp_path)
    _session(db, OTHER, str(CWD))
    _message(db, 1, _reply("gemma4:26b", 777), session=OTHER)  # newer, in the same cwd, other session
    adapter = OpenCodeAdapter(home=tmp_path, env={})
    assert adapter.read_context(_ref(db, SESSION)).used_tokens == 54542
    assert adapter.read_context(_ref(db, OTHER)).used_tokens == 777


def test_no_record_path_or_session_id_gives_no_reading(tmp_path):
    adapter = OpenCodeAdapter(home=tmp_path, env={})
    assert adapter.read_context(_ref(None)) is None
    assert adapter.read_context(_ref(_install(tmp_path), None)) is None


def test_unknown_model_uses_fallback_window(tmp_path):
    db = _install(tmp_path, config=False, catalogue=False)
    _message(db, 1, _reply("no-such-model", 5000, provider="nowhere"))
    reading = OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db))
    assert reading is not None and reading.used_tokens == 5000
    assert (reading.window_tokens, reading.window_source) == (128_000, "fallback")


def test_window_from_model_table_when_unrecorded(tmp_path):
    db = _install(tmp_path, config=False, catalogue=False)
    _message(db, 1, _reply("claude-opus-4-8", 5000, provider="anthropic"))
    reading = OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db))
    assert (reading.window_tokens, reading.window_source) == (200_000, "model_table")


def test_window_from_the_cached_catalogue_when_the_config_lists_none(tmp_path):
    db = _install(tmp_path)
    _message(db, 1, _reply("qwen3.5", 5000))  # only the catalogue knows ollama/qwen3.5
    reading = OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db))
    assert (reading.window_tokens, reading.window_source) == (262_144, "recorded")


def test_config_wins_over_the_catalogue(tmp_path):
    reading = _read(tmp_path)  # ollama/gemma4:26b: config says 131072, catalogue says 99999
    assert reading.window_tokens == 131_072


@pytest.mark.parametrize("order,window,source", [
    (("gemma4:26b", "qwen3.5"), 262_144, "recorded"),            # config model, then catalogue model
    (("qwen3.5", "gemma4:26b"), 131_072, "recorded"),
    (("claude-opus-4-8", "gemma4:26b"), 131_072, "recorded"),    # table model, then recorded model
    (("gemma4:26b", "claude-opus-4-8"), 200_000, "model_table"),
])
def test_window_belongs_to_the_model_of_the_latest_usage_message(tmp_path, order, window, source):
    db = _install(tmp_path)
    for number, model in enumerate(order, start=1):
        _message(db, number, _reply(model, 1000 * number, provider="anthropic" if model.startswith("claude") else "ollama"))
    reading = OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db))
    assert reading.used_tokens == 1000 * len(order)
    assert (reading.window_tokens, reading.window_source) == (window, source)


def test_malformed_window_entries_fall_through(tmp_path):
    db = _install(tmp_path)
    (tmp_path / ".config" / "opencode" / "opencode.json").write_text('{"provider": {"ollama": {"models": []}}}')
    (tmp_path / ".cache" / "opencode" / "models.json").write_text("{not json")
    reading = OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db))
    assert (reading.window_tokens, reading.window_source) == (128_000, "fallback")


def test_xdg_directories_come_from_the_given_env(tmp_path):
    data, config, cache = tmp_path / "d", tmp_path / "c", tmp_path / "k"
    (data / "opencode").mkdir(parents=True)
    _build(data / "opencode" / "opencode.db")
    (config / "opencode").mkdir(parents=True)
    shutil.copy(FIXTURES / "opencode.json", config / "opencode" / "opencode.json")
    env = {"XDG_DATA_HOME": str(data), "XDG_CONFIG_HOME": str(config), "XDG_CACHE_HOME": str(cache)}
    adapter = OpenCodeAdapter(home=tmp_path / "elsewhere", env=env)
    ref = adapter.locate_session(SESSION, CWD, "w1:p1")
    assert ref is not None and ref.record_path == data / "opencode" / "opencode.db"
    assert adapter.read_context(ref).window_tokens == 131_072


def test_locate_session_by_id(tmp_path):
    db = _install(tmp_path)
    ref = OpenCodeAdapter(home=tmp_path, env={}).locate_session(SESSION, CWD, "w1:p1")
    assert ref == SessionRef(Harness.OPENCODE, "w1:p1", CWD, SESSION, db)


def test_locate_session_picks_by_id_never_the_latest_session(tmp_path):
    db = _install(tmp_path)
    _session(db, OTHER, str(CWD))
    _message(db, 1, _reply("gemma4:26b", 777), session=OTHER)
    adapter = OpenCodeAdapter(home=tmp_path, env={})
    assert adapter.locate_session(SESSION, CWD, "w1:p1").session_id == SESSION
    assert adapter.locate_session(OTHER, CWD, "w1:p1").session_id == OTHER
    assert adapter.locate_session("ses_missing", CWD, "w1:p1") is None


def test_locate_session_without_id_needs_exactly_one_session_in_the_cwd(tmp_path):
    db = _install(tmp_path)
    adapter = OpenCodeAdapter(home=tmp_path, env={})
    assert adapter.locate_session(None, CWD, "w1:p1").session_id == SESSION
    assert adapter.locate_session(None, Path("/home/user/elsewhere"), "w1:p1") is None
    _session(db, "ses_sub", str(CWD), parent=SESSION)  # a subagent session does not count
    _session(db, "ses_old", str(CWD), archived=5)       # nor an archived one
    assert adapter.locate_session(None, CWD, "w1:p1").session_id == SESSION
    _session(db, OTHER, str(CWD))
    assert adapter.locate_session(None, CWD, "w1:p1") is None


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "a*", "a?", "a[b", "x' OR '1'='1"])
def test_locate_session_rejects_unsafe_ids(tmp_path, bad):
    _install(tmp_path)
    assert OpenCodeAdapter(home=tmp_path, env={}).locate_session(bad, CWD, "w1:p1") is None


def test_missing_database_gives_none_and_creates_nothing(tmp_path):
    adapter = OpenCodeAdapter(home=tmp_path, env={})
    assert adapter.locate_session(SESSION, CWD, "w1:p1") is None
    assert adapter.read_context(_ref(_db_path(tmp_path))) is None
    assert not (tmp_path / ".local").exists()
    assert list(tmp_path.rglob("*")) == []


def test_not_a_database_gives_none(tmp_path):
    bogus = tmp_path / "opencode.db"
    bogus.write_text("this is not sqlite")
    assert OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(bogus)) is None


def test_db_opened_read_only(tmp_path):
    """Rollback-journal mode (the fixture as built): a writer's open transaction does not stop a read-only reader."""
    db = _install(tmp_path)
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")  # a second real connection holds an open write transaction
    writer.execute("insert into message (id, session_id, time_created, time_updated, data) "
                   "values ('msg_held', ?, 3000000000000, 3000000000000, ?)", (SESSION, json.dumps(_reply("gemma4:26b", 1))))
    try:
        started = time.monotonic()
        reading = OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db))
        assert reading is not None and reading.used_tokens == 54542  # the uncommitted row is not visible
        assert time.monotonic() - started < 2
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    before = db.read_bytes()
    OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db))
    assert db.read_bytes() == before  # reading never writes
    readonly = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        readonly.execute("delete from message")
    readonly.close()


def test_locked_database_gives_none_quickly_not_an_exception(tmp_path):
    db = _install(tmp_path)
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")  # blocks readers of a rollback-journal database
    try:
        started = time.monotonic()
        adapter = OpenCodeAdapter(home=tmp_path, env={})
        assert adapter.read_context(_ref(db)) is None
        assert adapter.locate_session(SESSION, CWD, "w1:p1") is None
        assert time.monotonic() - started < 5
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_database_is_not_left_locked_after_reads(tmp_path):
    db = _install(tmp_path)
    adapter = OpenCodeAdapter(home=tmp_path, env={})
    adapter.read_context(_ref(db))
    adapter.read_context(_ref(db, "ses_missing"))
    adapter.locate_session(SESSION, CWD, "w1:p1")
    adapter.locate_session("bad/id", CWD, "w1:p1")
    locker = sqlite3.connect(db, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")  # an open reader would make this fail with "database is locked"
    locker.execute("ROLLBACK")
    locker.close()


def test_locate_without_herdr_on_path_falls_back_to_the_one_session_in_the_cwd(tmp_path):
    db = _install(tmp_path)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    adapter = OpenCodeAdapter(home=tmp_path, env={"PATH": str(empty)})  # a real environment with no herdr on PATH
    assert adapter.locate("w1:p1", CWD) == SessionRef(Harness.OPENCODE, "w1:p1", CWD, SESSION, db)
    _session(db, OTHER, str(CWD))
    assert adapter.locate("w1:p1", CWD) is None  # two sessions and no id from Herdr: refuse to guess


@pytest.mark.skipif(shutil.which("herdr") is None, reason="needs the real herdr binary")
def test_locate_with_a_pane_herdr_does_not_know_refuses_to_guess_between_sessions(tmp_path):
    db = _install(tmp_path)
    _session(db, OTHER, str(CWD))
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    assert OpenCodeAdapter(home=tmp_path, env=env).locate("w0:p0", CWD) is None


def test_drain_input_types_the_order_like_the_others():
    assert OpenCodeAdapter().drain_input("do it") == TypedInput("do it", ("Enter",))


def test_compact_and_exit_commands():
    adapter = OpenCodeAdapter()
    assert adapter.compact_input() == TypedInput("/compact", ("Enter",))
    assert adapter.exit_plan() == ExitPlan("/exit", ("Enter",), 15.0)


def test_ready_requires_status_and_the_placeholder_at_the_bottom():
    adapter = OpenCodeAdapter()
    screen = f"transcript\n\n  ┃  {PROMPT_PLACEHOLDER} \"Fix a TODO in the codebase\"\n  ┃  Build  gemma4:26b\n" + "\n" * 20
    assert adapter.is_ready_for_input(screen, "idle")
    assert adapter.is_ready_for_input(screen, "done")
    assert not adapter.is_ready_for_input(screen, "working")
    assert not adapter.is_ready_for_input(screen, "blocked")
    assert not adapter.is_ready_for_input("transcript\n  ┃  half typed order\n", "idle")
    scrolled_up = f"  ┃  {PROMPT_PLACEHOLDER}\n" + "output line\n" * 30  # an old placeholder far above live output
    assert not adapter.is_ready_for_input(scrolled_up, "idle")


def test_harness_and_registry():
    from herdr_engineering.handoff_adapters import adapter_for

    assert OpenCodeAdapter.harness is Harness.OPENCODE
    assert isinstance(adapter_for(Harness.OPENCODE), OpenCodeAdapter)


def test_fixture_and_test_carry_no_personal_data():
    for path in (FIXTURES / "opencode.sql", FIXTURES / "opencode.json", FIXTURES / "models.json", Path(__file__)):
        text = path.read_bytes().decode("latin-1")
        assert "/" + "Users/" not in text and "wohl" + "gemuth" not in text.lower()
    con = sqlite3.connect(":memory:")
    con.executescript((FIXTURES / "opencode.sql").read_text(encoding="utf-8"))
    try:
        assert con.execute("select count(*) from session").fetchone() == (1,)
        assert con.execute("select title, slug, directory from session").fetchone() == ("redacted", "redacted", "/home/user/proj")
    finally:
        con.close()


def _wal(db: Path) -> sqlite3.Connection:
    """A writer connection on the database in WAL mode (as OpenCode runs it), with checkpointing off."""
    writer = sqlite3.connect(db, isolation_level=None)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    return writer


def test_wal_reader_is_not_blocked_by_an_open_write_transaction(tmp_path):
    db = _install(tmp_path)
    writer = _wal(db)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("insert into message (id, session_id, time_created, time_updated, data) "
                   "values ('msg_held', ?, 3000000000000, 3000000000000, ?)", (SESSION, json.dumps(_reply("gemma4:26b", 1))))
    try:
        reading = OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db))
        assert reading is not None and reading.used_tokens == 54542  # the old committed state, not None, not the held row
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_wal_rows_committed_but_not_checkpointed_are_visible(tmp_path):
    db = _install(tmp_path)
    writer = _wal(db)
    try:
        writer.execute("insert into message (id, session_id, time_created, time_updated, data) "
                       "values ('msg_wal', ?, 3000000000000, 3000000000000, ?)", (SESSION, json.dumps(_reply("gemma4:26b", 61000))))
        assert (db.parent / "opencode.db-wal").stat().st_size > 0  # the row is in the -wal file only
        reading = OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db))
        assert reading is not None and reading.used_tokens == 61000
        assert OpenCodeAdapter(home=tmp_path, env={}).locate_session(SESSION, CWD, "w1:p1") is not None
    finally:
        writer.close()


def test_session_id_resolves_even_when_its_directory_differs_from_the_pane_cwd(tmp_path):
    db = _install(tmp_path)
    moved = Path("/home/user/elsewhere")  # the id comes from Herdr for this pane; the session was started in CWD
    ref = OpenCodeAdapter(home=tmp_path, env={}).locate_session(SESSION, moved, "w1:p1")
    assert ref == SessionRef(Harness.OPENCODE, "w1:p1", moved, SESSION, db)


def test_relative_xdg_data_home_is_resolved_not_raised(tmp_path, monkeypatch):
    (tmp_path / "rel" / "opencode").mkdir(parents=True)
    _build(tmp_path / "rel" / "opencode" / "opencode.db")
    monkeypatch.chdir(tmp_path)  # a relative path is taken against the working directory
    adapter = OpenCodeAdapter(home=tmp_path / "elsewhere", env={"XDG_DATA_HOME": "rel"})
    ref = adapter.locate_session(SESSION, CWD, "w1:p1")
    assert ref is not None
    assert adapter.read_context(ref).used_tokens == 54542


def test_total_of_zero_with_parts_sums_the_parts(tmp_path):
    db = _install(tmp_path)
    parts = {"input": 100, "output": 20, "reasoning": 5, "cache": {"read": 300, "write": 7}}
    _message(db, 1, {"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b", "tokens": {"total": 0, **parts}})
    assert OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db)).used_tokens == 432


def test_a_bool_is_not_a_token_count(tmp_path):
    db = _install(tmp_path)
    _message(db, 1, {"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b",
                     "tokens": {"total": True, "input": True, "output": 0, "cache": {"read": True, "write": 0}}})
    assert OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db)).used_tokens == 54542  # skipped as no usage


def test_database_without_a_message_table_gives_none(tmp_path):
    db = tmp_path / "other.db"
    con = sqlite3.connect(db)
    con.execute("create table unrelated (x integer)")
    con.commit()
    con.close()
    adapter = OpenCodeAdapter(home=tmp_path, env={"XDG_DATA_HOME": str(tmp_path / "d")})
    assert adapter.read_context(_ref(db)) is None


@pytest.mark.parametrize("payload", ["[1, 2]", "42", '"text"', "null", "not json"])
def test_message_data_that_is_not_an_object_is_skipped(tmp_path, payload):
    db = _install(tmp_path)
    con = sqlite3.connect(db)
    con.execute("insert into message (id, session_id, time_created, time_updated, data) "
                "values ('msg_odd', ?, 3000000000000, 3000000000000, ?)", (SESSION, payload))
    con.commit()
    con.close()
    assert OpenCodeAdapter(home=tmp_path, env={}).read_context(_ref(db)).used_tokens == 54542


def test_read_context_window_matches_recorded_field(tmp_path):
    reading = _read(tmp_path)  # the fixture's latest reply is ollama/gemma4:26b
    config = json.loads((FIXTURES / "opencode.json").read_text())
    assert reading is not None
    assert reading.window_tokens == config["provider"]["ollama"]["models"]["gemma4:26b"]["limit"]["context"]
    assert reading.window_source == "recorded"


# screens captured from a real opencode pane (`herdr pane read --format ansi`), 2026-11-18, redacted
REAL_EMPTY = (FIXTURES / "screen_empty.ansi").read_text(encoding="utf-8")
REAL_TYPED = (FIXTURES / "screen_typed.ansi").read_text(encoding="utf-8")


@pytest.mark.parametrize("status", ["idle", "done"])
def test_real_screen_with_an_empty_prompt_is_ready(status):
    assert OpenCodeAdapter().is_ready_for_input(REAL_EMPTY, status)


def test_real_screen_with_typed_text_is_not_ready():
    assert not OpenCodeAdapter().is_ready_for_input(REAL_TYPED, "idle")


@pytest.mark.parametrize("status", ["working", "blocked", "unknown"])
def test_real_empty_screen_is_not_ready_while_the_harness_is_busy(status):
    assert not OpenCodeAdapter().is_ready_for_input(REAL_EMPTY, status)
