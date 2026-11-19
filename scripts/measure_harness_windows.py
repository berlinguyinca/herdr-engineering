#!/usr/bin/env python3
"""Measure, from real session records, where each agent harness records a context window.

Read-only. Per harness it prints where a window or model limit appears (file kind and field name, or an
explicit "none found"), then the distinct model ids seen in recent sessions with the window each resolves to
and its window_source, then any session whose latest reading is above 100% of its resolved window (a signal
that the window is wrong). It prints no message text, file contents, user names or absolute home paths: only
field names, model ids, numbers and globs shortened with "~".

Usage:
  measure_harness_windows.py [--home DIR] [--days N]       (DIR defaults to $HERDR_MEASURE_HOME, then $HOME)
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import tomllib
from collections import defaultdict
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from herdr_engineering.handoff_adapters import claude, codex, opencode, pi  # noqa: E402
from herdr_engineering.handoff_adapters.base import ContextReading, Harness, SessionRef  # noqa: E402
from herdr_engineering.handoff_adapters.records import records_newest_first  # noqa: E402

_WINDOW_KEY = re.compile(r"(?i)window(?!s)|context_?size|context_?limit|max_?input|max_?context")
_SCHEMA_KEYS = frozenset({"input_schema", "properties", "schema"})  # tool definitions list parameter names, not limits
# Structural field names that may be printed as they are. Any other intermediate key (a provider, project, flag or
# model name, even a plain word) is shown as "*"; a key that itself looks like a window or limit is a leaf we report.
_KNOWN_KEYS = frozenset({"cachedGrowthBookFeatures", "catalog", "config", "models", "providers", "provider", "limit",
                         "payload", "info", "rate_limits", "primary", "secondary", "state", "runtime"})
_MAX_SESSIONS = 200  # newest sessions per harness
_TAIL_RECORDS = 300  # newest records read from a session file when listing its field names
_FALLBACK = "-"


def _field_paths(value: object, found: dict[str, set[int]], path: str = "") -> None:
    """Collect the key paths whose last key looks like a window or limit, with the integers seen there.

    A key that is not a known field name or a window-like leaf key (a model id, uuid, project path, provider or flag
    name, or any non-leaf key that merely contains "window"), or that names a model, is shown as "*", so no id or path is printed. A `context` key counts only inside a `limit` object (OpenCode's limit.context).
    """
    if isinstance(value, dict):
        for key, child in value.items():
            leaf = not isinstance(child, (dict, list))  # only a leaf may be shown for what its name contains
            plain = (isinstance(key, str) and not path.endswith("models") and not re.search(r"[^A-Za-z_]", key)
                     and (key in _KNOWN_KEYS or (leaf and (bool(_WINDOW_KEY.search(key)) or key == "context"))))
            name = key if plain else "*"
            if name in _SCHEMA_KEYS:
                continue
            here = f"{path}.{name}" if path else name
            if _WINDOW_KEY.search(name) or (name == "context" and path.endswith("limit")):
                numbers = found.setdefault(here, set())
                if isinstance(child, int) and not isinstance(child, bool):
                    numbers.add(child)
            _field_paths(child, found, here)
    elif isinstance(value, list):
        for child in value:
            _field_paths(child, found, f"{path}[]")


def _describe(found: dict[str, set[int]]) -> str:
    if not found:
        return "none found"
    return "; ".join(f"{path}" + (f" {_numbers(numbers)}" if numbers else "") for path, numbers in sorted(found.items()))


def _numbers(numbers: set[int]) -> str:
    ordered = sorted(numbers)
    return str(ordered) if len(ordered) <= 4 else f"{len(ordered)} values, {ordered[0]}..{ordered[-1]}"


def _json_file(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _report_json(label: str, paths: list[Path], keyed_by_provider: bool = False) -> str:
    found: dict[str, set[int]] = {}
    for path in paths:
        data = _json_file(path)
        if keyed_by_provider and isinstance(data, dict):
            data = list(data.values())  # the top-level keys are provider names; show them as []
        if data is not None:
            _field_paths(data, found)
    return f"  {label}: {_describe(found)}"


def _report_records(label: str, files: list[Path]) -> str:
    found: dict[str, set[int]] = {}
    for path in files:
        try:
            for _, record in zip(range(_TAIL_RECORDS), records_newest_first(path)):
                _field_paths(record, found)
        except OSError:
            continue
    return f"  {label}: {_describe(found)}"


def _recent(files: list[Path], cutoff: float) -> list[Path]:
    fresh = [(p.stat().st_mtime, p) for p in files if p.is_file()]
    return [p for mtime, p in sorted(fresh, reverse=True) if mtime >= cutoff][:_MAX_SESSIONS]


def _claude_fields(home: Path, cutoff: float, sessions: list[Path]) -> list[str]:
    root = home / ".claude"
    settings = _json_file(root / "settings.json")
    statusline = "configured" if isinstance(settings, dict) and "statusLine" in settings else "not configured"
    return [
        _report_records("transcript ~/.claude/projects/*/<session>.jsonl", sessions),
        _report_json("model catalogue ~/.claude/cache/model-catalog/*-cc.json",
                     sorted((root / "cache" / "model-catalog").glob("*-cc.json"))),
        _report_json("global config ~/.claude.json", [home / ".claude.json"]),
        f"  statusLine in ~/.claude/settings.json: {statusline}",
        _report_json("statusline input cache ~/.claude/hud/cache/stdin.*.json", sorted((root / "hud" / "cache").glob("stdin.*.json"))),
    ]


def _codex_fields(home: Path, cutoff: float, sessions: list[Path]) -> list[str]:
    found: dict[str, set[int]] = {}
    try:
        _field_paths(tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8")), found)
    except (OSError, ValueError):
        pass
    return [_report_records("rollout ~/.codex/sessions/Y/M/D/rollout-*.jsonl", sessions),
            f"  config ~/.codex/config.toml: {_describe(found)}"]


def _pi_fields(home: Path, cutoff: float, sessions: list[Path]) -> list[str]:
    agent = home / ".pi" / "agent"
    return [_report_records("session ~/.pi/agent/sessions/*/<ts>_<uuid>.jsonl", sessions),
            _report_json("models ~/.pi/agent/models.json", [agent / "models.json"]),
            _report_json("model store ~/.pi/agent/models-store.json", [agent / "models-store.json"], keyed_by_provider=True)]


def _opencode_db(home: Path, env: dict[str, str]) -> Path:
    base = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
    return base / "opencode" / "opencode.db"


def _opencode_fields(home: Path, cutoff: float, db: Path) -> list[str]:
    found: dict[str, set[int]] = {}
    columns: list[str] = []
    try:
        with closing(opencode.connect(db)) as con:
            for table in ("session", "session_context_epoch"):
                columns += [f"{table}.{row[1]}" for row in con.execute(f"pragma table_info({table})")
                            if _WINDOW_KEY.search(row[1])]
            for (raw,) in con.execute("select data from message order by time_created desc limit ?", (_TAIL_RECORDS,)):
                try:
                    _field_paths(json.loads(raw), found)
                except (TypeError, ValueError):
                    continue
    except sqlite3.Error:
        pass
    lines = [f"  database ~/.local/share/opencode/opencode.db, message.data: {_describe(found)}",
             f"  database columns of session, session_context_epoch: {', '.join(columns) or 'none found'}"]
    lines.append(_report_json("config ~/.config/opencode/opencode.json", [home / ".config" / "opencode" / "opencode.json"]))
    lines.append(_report_json("model catalogue ~/.cache/opencode/models.json", [home / ".cache" / "opencode" / "models.json"],
                             keyed_by_provider=True))
    return lines


def _readings(harness: Harness, home: Path, env: dict[str, str], files: list[Path], cutoff: float
              ) -> Iterator[tuple[str, ContextReading]]:
    """(provider/model, latest reading) of each recent session that has usage."""
    if harness is Harness.OPENCODE:
        db = _opencode_db(home, env)
        try:
            with closing(opencode.connect(db)) as con:
                ids = [r[0] for r in con.execute("select id from session where time_updated >= ? order by time_updated desc limit ?",
                                                 (int(cutoff * 1000), _MAX_SESSIONS))]
        except sqlite3.Error:
            return
        refs = [SessionRef(harness, "-", home, sid, db) for sid in ids]
    else:
        refs = [SessionRef(harness, "-", home, None, path) for path in files]
    adapter = {Harness.CLAUDE: claude.ClaudeAdapter, Harness.CODEX: codex.CodexAdapter, Harness.PI: pi.PiAdapter,
               Harness.OPENCODE: opencode.OpenCodeAdapter}[harness](home=home, **({"env": env} if harness is Harness.OPENCODE else {}))
    for ref in refs:
        try:
            reading = adapter.read_context(ref)
            model = _model_of(harness, ref)
        except (OSError, sqlite3.Error):
            continue
        if reading is not None:
            yield model, reading


def _model_of(harness: Harness, ref: SessionRef) -> str:
    assert ref.record_path is not None
    if harness is Harness.CLAUDE:
        found = claude.last_usage(ref.record_path)
        return found[1] or _FALLBACK if found else _FALLBACK
    if harness is Harness.CODEX:
        for record in records_newest_first(ref.record_path):
            if model := codex.turn_model(record):
                return model
        return _FALLBACK
    if harness is Harness.PI:
        found = pi.last_usage(ref.record_path)
        return f"{found[1]}/{found[2]}" if found else _FALLBACK
    assert ref.session_id is not None
    found = opencode.last_usage(ref.record_path, ref.session_id)
    return f"{found[1]}/{found[2]}" if found else _FALLBACK


def _models_report(harness: Harness, home: Path, env: dict[str, str], files: list[Path], cutoff: float) -> list[str]:
    latest: dict[str, ContextReading] = {}
    count: dict[str, int] = defaultdict(int)
    over: list[str] = []
    for model, reading in _readings(harness, home, env, files, cutoff):
        latest.setdefault(model, reading)  # newest session first
        count[model] += 1
        if reading.percent > 100:
            over.append(f"  OVER 100%: {model} used={reading.used_tokens} window={reading.window_tokens} "
                        f"window_source={reading.window_source}")
    lines = ["models seen in recent sessions (window and window_source as the adapter resolves them):"]
    lines += [f"  {model}  window={r.window_tokens}  window_source={r.window_source}  sessions={count[model]}"
              for model, r in sorted(latest.items())] or ["  none (no recent session with usage)"]
    lines.append("sessions above 100% of their window:")
    lines += over or ["  none"]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("HERDR_MEASURE_HOME") or Path.home()))
    parser.add_argument("--days", type=float, default=30.0)
    args = parser.parse_args(argv)
    home: Path = args.home
    cutoff = time.time() - args.days * 86400
    env = {k: v for k, v in os.environ.items() if k.startswith("XDG_")} if args.home == Path.home() else {}

    files = {
        Harness.CLAUDE: _recent(list((home / ".claude" / "projects").glob("*/*.jsonl")), cutoff),
        Harness.CODEX: _recent(list((home / ".codex" / "sessions").glob("*/*/*/rollout-*.jsonl")), cutoff),
        Harness.PI: _recent(list((home / ".pi" / "agent" / "sessions").glob("*/*.jsonl")), cutoff),
        Harness.OPENCODE: [],
    }
    fields = {
        Harness.CLAUDE: lambda: _claude_fields(home, cutoff, files[Harness.CLAUDE]),
        Harness.CODEX: lambda: _codex_fields(home, cutoff, files[Harness.CODEX]),
        Harness.PI: lambda: _pi_fields(home, cutoff, files[Harness.PI]),
        Harness.OPENCODE: lambda: _opencode_fields(home, cutoff, _opencode_db(home, env)),
    }
    for harness in Harness:
        print(f"== {harness.value} (last {args.days:g} days) ==")
        print("where a window or model limit is recorded:")
        print("\n".join(fields[harness]()))
        print("\n".join(_models_report(harness, home, env, files[harness], cutoff)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
