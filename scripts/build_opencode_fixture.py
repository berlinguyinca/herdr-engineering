#!/usr/bin/env python3
"""Build the redacted OpenCode fixture database from one real session.

Copies the schema of the `session` and `message` tables and that session's rows into a new database,
keeping only ids (rewritten to synthetic ones), roles, timestamps, provider/model ids, finish reasons,
the compaction flags and the token counts. Every text, title, path and diff value becomes "redacted".
The source database is opened read-only.

Usage:
  build_opencode_fixture.py --source ~/.local/share/opencode/opencode.db --session ses_... --out opencode.db
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

FIXTURE_SESSION = "ses_fixture0001"
FIXTURE_DIRECTORY = "/home/user/proj"


def _redact_message(data: dict) -> dict:
    kept: dict = {"role": data.get("role")}
    if data.get("role") != "assistant":
        return kept
    for key in ("providerID", "modelID", "finish", "tokens"):
        if key in data:
            kept[key] = data[key]
    kept["mode"] = "compaction" if data.get("mode") == "compaction" else "build"
    if data.get("summary") is True or data.get("summary") == 1:
        kept["summary"] = True
    return kept


def build(source: Path, session_id: str, out: Path) -> None:
    if out.exists():
        raise SystemExit(f"{out} exists; refusing to overwrite")
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    try:
        for (sql,) in src.execute("select sql from sqlite_master where type='table' and name in ('session','message')"):
            dst.execute(sql)
        for (sql,) in src.execute(
                "select sql from sqlite_master where type='index' and sql is not null and tbl_name in ('session','message')"):
            dst.execute(sql)
        row = src.execute("select time_created, time_updated from session where id=?", (session_id,)).fetchone()
        if row is None:
            raise SystemExit("no such session in the source database")
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
