"""Smoke test: scripts/measure_harness_windows.py run as a real subprocess against the committed fixtures."""
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "handoff_adapters"
SCRIPT = REPO / "scripts" / "measure_harness_windows.py"


def _home(root: Path, catalog: str = "model-catalog.json") -> Path:
    home = root / "home"
    claude = home / ".claude"
    (claude / "projects" / "-work").mkdir(parents=True)
    shutil.copy(FIXTURES / "claude" / "main_chain.jsonl", claude / "projects" / "-work" / "s.jsonl")
    (claude / "cache" / "model-catalog").mkdir(parents=True)
    shutil.copy(FIXTURES / "claude" / catalog, claude / "cache" / "model-catalog" / "org-x-cc.json")
    codex = home / ".codex" / "sessions" / "2026" / "11" / "13"
    codex.mkdir(parents=True)
    shutil.copy(FIXTURES / "codex" / "main.jsonl", codex / "rollout-2026-11-13T13-45-32-00000000-0000-7000-8000-000000000001.jsonl")
    pi = home / ".pi" / "agent"
    (pi / "sessions" / "--work--").mkdir(parents=True)
    shutil.copy(FIXTURES / "pi" / "main.jsonl", pi / "sessions" / "--work--" / "2026-11-17T18-08-38-549Z_11111111-1111-7111-8111-111111111111.jsonl")
    shutil.copy(FIXTURES / "pi" / "models.json", pi / "models.json")
    db = home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript((FIXTURES / "opencode" / "opencode.sql").read_text(encoding="utf-8"))
    con.close()
    (home / ".config" / "opencode").mkdir(parents=True)
    shutil.copy(FIXTURES / "opencode" / "opencode.json", home / ".config" / "opencode" / "opencode.json")
    return home


def _run(home: Path) -> str:
    done = subprocess.run([sys.executable, str(SCRIPT), "--home", str(home), "--days", "100000"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_prints_a_section_per_harness_with_sources_models_and_the_over_100_check(tmp_path):
    out = _run(_home(tmp_path))
    for harness in ("claude", "codex", "pi", "opencode"):
        assert f"== {harness} (last 100000 days) ==" in out
    assert "transcript ~/.claude/projects/*/<session>.jsonl: none found" in out
    assert "catalog.config.models[].runtime.max_input_tokens [200000, 1000000]" in out
    assert "payload.info.model_context_window [828400]" in out
    assert "providers.*.models[].contextWindow" in out
    assert "limit.context" in out
    assert "  claude-opus-4-8  window=200000  window_source=model_table\n" in out  # catalogue 1000000 vs table 200000: the smaller
    assert "  gpt-6.1-sol  window=828400  window_source=recorded\n" in out
    assert "  */qwen3.8-flash_next  window=131072  window_source=recorded\n" in out
    assert re.search(r"  \*/gemma4:26b  window=131072  window_source=recorded\n", out)
    assert out.count("sessions above 100% of their window:\n  none") == 4


def test_prints_no_absolute_path_session_id_or_message_text(tmp_path):
    home = _home(tmp_path)
    out = _run(home)
    assert str(tmp_path) not in out and str(home) not in out
    assert "11111111-1111" not in out and "ses_fixture" not in out
    assert "redacted" not in out


def test_flags_a_session_above_100_percent_of_its_window(tmp_path):
    home = _home(tmp_path)
    catalog = home / ".claude" / "cache" / "model-catalog" / "org-x-cc.json"
    data = json.loads(catalog.read_text())
    data["catalog"]["config"]["models"][0]["id"] = "claude-future-9"  # not in the table, so only the catalogue sets the window
    data["catalog"]["config"]["models"][0]["runtime"]["max_input_tokens"] = 1000  # real usage in the fixture is 5702
    catalog.write_text(json.dumps(data))
    transcript = home / ".claude" / "projects" / "-work" / "s.jsonl"
    transcript.write_text(transcript.read_text().replace("claude-opus-4-8", "claude-future-9"))
    out = _run(home)
    assert "  OVER 100%: claude-future-9 used=5702 window=1000 window_source=recorded" in out


def test_a_home_with_no_records_prints_none_found_not_an_error(tmp_path):
    out = _run(tmp_path)
    assert out.count("none found") >= 4
    assert out.count("none (no recent session with usage)") == 4


def test_an_intermediate_key_that_is_a_plain_word_is_masked(tmp_path):
    home = _home(tmp_path)
    (home / ".claude.json").write_text(json.dumps({"acmeproject": {"contextWindow": 5}, "tengu_secret_flag": {"windowDays": 7}}))
    out = _run(home)
    assert "acmeproject" not in out and "tengu_secret_flag" not in out
    assert "*.contextWindow [5]" in out and "*.windowDays [7]" in out
    assert "providers.example" not in out and "provider.example" not in out


def test_an_intermediate_key_that_contains_window_or_context_size_is_masked(tmp_path):
    home = _home(tmp_path)
    (home / ".claude.json").write_text(json.dumps({"acme-window-corp": {"contextWindow": 5}, "model_context_size_x": {"windowDays": 7}}))
    out = _run(home)
    assert "acme-window-corp" not in out and "model_context_size_x" not in out
    assert "*.contextWindow [5]" in out and "*.windowDays [7]" in out


def test_prints_no_provider_name_and_no_session_count_in_the_models_lines(tmp_path):
    out = _run(_home(tmp_path))
    assert "ollama" not in out and "example/" not in out
    assert "sessions=" not in out
    assert "  */qwen3.8-flash_next  window=131072" in out
