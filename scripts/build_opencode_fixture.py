#!/usr/bin/env python3
"""Build the redacted OpenCode fixture (a SQL dump) from one real session.

Dumps the schema of the `session` and `message` tables and that session's rows,
keeping only ids (rewritten to synthetic ones), roles, timestamps, provider/model ids, finish reasons,
`mode`/`summary` (the compaction markers) and the token counts. Every text, title, path and diff value becomes "redacted".
The source database is opened read-only.

Usage:
  build_opencode_fixture.py --source ~/.local/share/opencode/opencode.db --session ses_... --out opencode.sql
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

FIXTURE_SESSION = "ses_fixture0001"
FIXTURE_DIRECTORY = "/home/user/proj"


def _redact_tokens(tokens: object) -> dict:
    if not isinstance(tokens, dict):
        return {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    kept: dict = {key: tokens[key] for key in ("total", "input", "output", "reasoning") if key in tokens}
    kept["cache"] = {key: cache[key] for key in ("read", "write") if key in cache}
    return kept


def _redact_message(data: dict) -> dict:
    """Whitelisted keys only; `mode` and `summary` (the compaction markers) keep their real values."""
    kept: dict = {"role": data.get("role")}
    if data.get("role") != "assistant":
        return kept
    for key in ("providerID", "modelID", "finish", "mode", "summary"):
        if key in data:
            kept[key] = data[key]
    if "tokens" in data:
        kept["tokens"] = _redact_tokens(data["tokens"])
    return kept


def build(source: Path, session_id: str, out: Path) -> None:
    """Write a SQL dump (schema plus the redacted rows) to `out`; nothing is written if the session is missing."""
    if out.exists():
        raise SystemExit(f"{out} exists; refusing to overwrite")
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(":memory:")
    try:
        row = src.execute("select time_created, time_updated from session where id=?", (session_id,)).fetchone()
        if row is None:
            raise SystemExit("no such session in the source database")
        for (sql,) in src.execute("select sql from sqlite_master where type='table' and name in ('session','message')"):
            dst.execute(sql)
        for (sql,) in src.execute(
                "select sql from sqlite_master where type='index' and sql is not null and tbl_name in ('session','message')"):
            dst.execute(sql)
        dst.execute(
            "insert into session (id, project_id, slug, directory, title, version, time_created, time_updated) "
            "values (?, 'redacted', 'redacted', ?, 'redacted', 'redacted', ?, ?)",
            (FIXTURE_SESSION, FIXTURE_DIRECTORY, *row))
        rows = src.execute("select time_created, time_updated, data from message where session_id=? order by time_created, id",
                           (session_id,))
        for number, (created, updated, data) in enumerate(rows, start=1):
            dst.execute("insert into message (id, session_id, time_created, time_updated, data) values (?, ?, ?, ?, ?)",
                        (f"msg_fixture{number:04d}", FIXTURE_SESSION, created, updated, json.dumps(_redact_message(json.loads(data)))))
        dst.commit()
        out.write_text("".join(f"{line}\n" for line in dst.iterdump()), encoding="utf-8")
    finally:
        src.close()
        dst.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    build(args.source, args.session, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
