"""Read-only session history for the ``summary`` skill.

Resolves the *current* agent session and returns a normalized, redacted event
list plus an evidence-coverage verdict, for Claude Code, Pi, Codex and
OpenCode. It reuses the transcript locations ``agent_mine`` already knows and
the Herdr pane metadata (``herdr pane get``); it keeps no store of its own and
never writes. Anything it cannot read is reported in ``coverage.gaps`` rather
than guessed.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .control_plane.redact import redact_text

AGENTS = ("claude", "pi", "codex", "opencode")
TEXT_LIMIT = 400
# Tool names that start a child agent in each harness; only their final result
# is in the parent transcript unless the child's own record is found.
_CHILD_TOOLS = {"task", "agent", "subagent", "spawn_agent", "delegate"}


@dataclass
class Resolution:
    agent: str | None = None
    path: str | None = None
    session_id: str | None = None
    method: str = "none"  # explicit | herdr-pane | newest-in-cwd (heuristic) | none
    candidates: list[str] = field(default_factory=list)
    note: str = ""


def _stores(home: Path) -> dict[str, Path]:
    return {
        "claude": home / ".claude/projects",
        "pi": home / ".pi/agent/sessions",
        "codex": home / ".codex/sessions",
        "opencode": home / ".local/share/opencode/opencode.db",
    }


def _clip(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = redact_text(" ".join(text.split()))
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + "…"


def _jsonl(path: str | Path) -> Iterator[dict]:
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _blocks_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


# --- session resolution -----------------------------------------------------

def _pane_session(env: dict[str, str], herdr_bin: str | None) -> dict | None:
    pane = env.get("HERDR_PANE_ID")
    exe = herdr_bin or shutil.which("herdr")
    if not pane or not exe:
        return None
    try:
        out = subprocess.run([exe, "pane", "get", pane], capture_output=True, text=True, timeout=10)
        info = json.loads(out.stdout)["result"]["pane"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None
    sess = info.get("agent_session")
    if isinstance(sess, dict):
        return {"agent": info.get("agent"), **sess}
    # Herdr reports no conversation for some agents (idle Codex); keep the pane so the
    # process-keyed fallback below can still try, instead of guessing from the directory.
    return {"agent": info.get("agent"), "pane": pane, "exe": exe} if info.get("agent") else None


def _foreground_pids(pane: str, exe: str) -> list[int]:
    try:
        out = subprocess.run([exe, "pane", "process-info", "--pane", pane], capture_output=True, text=True, timeout=10)
        rows = json.loads(out.stdout)["result"]["process_info"]["foreground_processes"]
        return [int(r["pid"]) for r in rows if int(r["pid"]) > 0][:32]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return []


def _process_start_seconds(pid: int, proc: Path = Path("/proc")) -> float | None:
    try:
        stat = (proc / str(pid) / "stat").read_text()
        ticks = int(stat[stat.rindex(")") + 1:].split()[19])
        boot = int(next(l.split()[1] for l in (proc / "stat").read_text().splitlines() if l.startswith("btime ")))
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return boot + ticks / 100


def codex_rollout_for_pids(home: Path, pids: list[int], proc: Path = Path("/proc")) -> str | None:
    """Idle Codex holds no rollout open; its own log database records the owning pid and thread.

    Same evidence rule as the dashboard plugin: rows must be at least as new as the process (a reused
    pid cannot inherit an older conversation), only a primary conversation counts, and a tie is not guessed.
    """
    dbs = sorted(glob.glob(str(home / ".codex" / "logs_*.sqlite")), reverse=True,
                 key=lambda p: int(p.rsplit("_", 1)[1].split(".")[0]) if p.rsplit("_", 1)[1].split(".")[0].isdigit() else -1)
    if not dbs:
        return None
    found: dict[str, int] = {}
    try:
        con = sqlite3.connect(f"file:{dbs[0]}?mode=ro", uri=True, timeout=1)
        try:
            for pid in pids:
                started = _process_start_seconds(pid, proc)
                if started is None:
                    continue
                for thread, last in con.execute(
                        "select thread_id, max(ts) from logs where process_uuid like ? and thread_id is not null "
                        "and ts >= ? group by thread_id order by 2 desc limit 16", (f"pid:{pid}:%", int(started) - 2)):
                    for f in glob.glob(str(home / ".codex/sessions/**" / f"rollout-*{thread}.jsonl"), recursive=True):
                        head = next(_jsonl(f), {})
                        if head.get("type") == "session_meta" and (head.get("payload") or {}).get("id") == thread \
                                and isinstance((head.get("payload") or {}).get("source"), str):
                            found[f] = max(found.get(f, 0), int(last))
        finally:
            con.close()
    except sqlite3.Error:
        return None
    ranked = sorted(found.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][1] == ranked[1][1]):
        return None
    return ranked[0][0]


def _find_by_id(home: Path, agent: str, sid: str) -> str | None:
    store = _stores(home)[agent]
    if agent == "opencode":
        return str(store) if store.exists() else None
    for f in glob.glob(str(store / "**" / "*.jsonl"), recursive=True):
        if sid in os.path.basename(f) and "subagents" not in f:
            return f
    return None


def _newest_in_cwd(home: Path, agent: str, cwd: str) -> list[str]:
    store = _stores(home)[agent]
    if agent == "opencode":
        return []
    key = cwd.replace("/", "-")
    found = []
    for f in glob.glob(str(store / "**" / "*.jsonl"), recursive=True):
        if "subagents" in f:
            continue
        if agent == "claude" and os.path.basename(os.path.dirname(f)) != key:
            continue
        if agent == "pi" and os.path.basename(os.path.dirname(f)) != f"--{key[1:]}--":
            continue
        found.append(f)
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found[:5]


def resolve(*, agent: str | None = None, session: str | None = None, cwd: str | None = None,
            home: Path | None = None, env: dict[str, str] | None = None,
            herdr_bin: str | None = None, proc: Path = Path("/proc")) -> Resolution:
    """Pick the session: explicit id/path, then Herdr pane metadata, then a heuristic."""
    home = home or Path.home()
    env = dict(os.environ if env is None else env)
    cwd = cwd or os.getcwd()
    if session and os.path.isfile(session):
        return Resolution(agent=agent, path=session, session_id=Path(session).stem, method="explicit")
    if session and agent not in AGENTS:
        # An id with no agent: look in every store rather than fall to the directory heuristic.
        for candidate in AGENTS:
            path = _find_by_id(home, candidate, session) if candidate != "opencode" else None
            if path:
                return Resolution(agent=candidate, path=path, session_id=session, method="explicit")
    if session and agent in AGENTS:
        path = _find_by_id(home, agent, session)
        return Resolution(agent=agent, path=path, session_id=session, method="explicit",
                          note="" if path else "session id not found in the local store")
    pane = _pane_session(env, herdr_bin)
    if pane and pane.get("agent") == "codex" and not pane.get("value") and pane.get("pane"):
        path = codex_rollout_for_pids(home, _foreground_pids(pane["pane"], pane["exe"]), proc)
        if path:
            return Resolution(agent="codex", path=path, session_id=Path(path).stem.split("-", 6)[-1],
                              method="herdr-pane (codex process log)")
        return Resolution(agent="codex", method="herdr-pane (codex process log)",
                          note="Herdr reports no Codex conversation for this pane and Codex's own process log did not identify one")
    if pane and pane.get("agent") in AGENTS and pane.get("value"):
        value = str(pane["value"])
        path = value if pane.get("kind") == "file" and os.path.isfile(value) else _find_by_id(
            home, pane["agent"], value)
        return Resolution(agent=pane["agent"], path=path, session_id=value, method="herdr-pane",
                          note="" if path else "Herdr named the session but its record was not found")
    agents = [agent] if agent in AGENTS else list(AGENTS)
    cands = [(a, f) for a in agents for f in _newest_in_cwd(home, a, cwd)]
    cands.sort(key=lambda af: os.path.getmtime(af[1]), reverse=True)
    if not cands:
        return Resolution(note="no session record found for this directory")
    top = cands[0]
    return Resolution(agent=top[0], path=top[1], session_id=Path(top[1]).stem, method="newest-in-cwd (heuristic)",
                      candidates=[f for _, f in cands[:5]],
                      note="no explicit id or Herdr pane metadata; newest record in this directory chosen")


# --- per-agent normalizers --------------------------------------------------

def _ev(kind: str, text: Any = "", ts: Any = None, **extra: Any) -> dict:
    return {"kind": kind, "ts": ts, "text": _clip(text), **extra}


def _claude(path: str) -> tuple[list[dict], dict]:
    events: list[dict] = []
    meta = {"child_records": 0, "child_detail": False}
    names: dict[str, str] = {}
    for d in _jsonl(path):
        ts = d.get("timestamp")
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        if d.get("type") == "system" and "compact" in str(d.get("subtype", "")):
            events.append(_ev("compaction", d.get("content") or d.get("subtype"), ts))
            continue
        if d.get("type") not in ("user", "assistant"):
            continue
        if d.get("isSidechain"):
            meta["child_records"] += 1
            meta["child_detail"] = True
            continue
        content = msg.get("content")
        if d.get("isCompactSummary"):
            events.append(_ev("compaction", _blocks_text(content) or content, ts))
            continue
        if isinstance(content, str):
            events.append(_ev(d["type"], content, ts))
            continue
        for b in content or []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                events.append(_ev(d["type"], b.get("text"), ts))
            elif t == "tool_use":
                names[b.get("id")] = str(b.get("name"))
                kind = "child_dispatch" if str(b.get("name")).lower() in _CHILD_TOOLS else "tool_call"
                inp = b.get("input") or {}
                events.append(_ev(kind, inp.get("command") or inp.get("description") or inp, ts, tool=b.get("name")))
            elif t == "tool_result":
                events.append(_ev("tool_result", _blocks_text(b.get("content")) or b.get("content"), ts,
                                  tool=names.get(b.get("tool_use_id")), ok=not b.get("is_error")))
    sub = Path(path).with_suffix("") / "subagents"
    if sub.is_dir():
        files = list(sub.glob("*.jsonl"))
        meta["child_records"] += len(files)
        meta["child_detail"] = meta["child_detail"] or bool(files)
    return events, meta


def _pi(path: str) -> tuple[list[dict], dict]:
    events: list[dict] = []
    names: dict[str, str] = {}
    for d in _jsonl(path):
        ts = d.get("timestamp")
        if d.get("type") == "compaction":
            events.append(_ev("compaction", d.get("summary"), ts, tokens_before=d.get("tokensBefore")))
            continue
        m = d.get("message")
        if d.get("type") != "message" or not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("user", "assistant"):
            c = m.get("content")
            if isinstance(c, str):
                events.append(_ev(role, c, ts))
                continue
            for b in c or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    events.append(_ev(role, b.get("text"), ts))
                elif b.get("type") == "toolCall":
                    names[b.get("id")] = str(b.get("name"))
                    args = b.get("arguments") or {}
                    kind = "child_dispatch" if str(b.get("name")).lower() in _CHILD_TOOLS else "tool_call"
                    events.append(_ev(kind, args.get("command") or args, ts, tool=b.get("name")))
        elif role == "toolResult":
            events.append(_ev("tool_result", _blocks_text(m.get("content")), ts,
                              tool=m.get("toolName") or names.get(m.get("toolCallId")), ok=not m.get("isError")))
    return events, {"child_records": 0, "child_detail": False}


def _codex(path: str) -> tuple[list[dict], dict]:
    events: list[dict] = []
    meta = {"child_records": 0, "child_detail": False}
    pending: list[str] = []
    for d in _jsonl(path):
        ts = d.get("timestamp")
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        t = p.get("type")
        if d.get("type") == "compacted" or t == "compacted":
            events.append(_ev("compaction", p.get("message") or "context compacted", ts))
        elif d.get("type") == "inter_agent_communication_metadata":
            meta["child_records"] += 1
        elif d.get("type") != "response_item":
            continue
        elif t == "message" and p.get("role") in ("user", "assistant"):
            events.append(_ev(p["role"], _blocks_text(p.get("content")), ts))
        elif t in ("function_call", "custom_tool_call"):
            pending.append(str(p.get("name")))
            raw = p.get("arguments") or p.get("input") or ""
            events.append(_ev("tool_call", raw, ts, tool=p.get("name")))
        elif t in ("function_call_output", "custom_tool_call_output"):
            out = p.get("output")
            text = out if isinstance(out, str) else json.dumps(out, default=str)
            failed = ("exit code: 0" not in text.lower() and "exit_code\":0" not in text.replace(" ", "")
                      and ("exit code" in text.lower() or "error" in text.lower()[:80]))
            events.append(_ev("tool_result", text, ts, tool=pending.pop(0) if pending else None, ok=not failed))
        elif t == "agent_message":
            meta["child_records"] += 1
            events.append(_ev("child_message", p.get("message") or p.get("content"), ts))
    return events, meta


def _opencode(db: str, sid: str | None) -> tuple[list[dict], dict]:
    events: list[dict] = []
    meta = {"child_records": 0, "child_detail": False}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return events, meta
    try:
        if not sid:
            row = con.execute("select id from session where parent_id is null order by time_updated desc limit 1").fetchone()
            sid = row[0] if row else None
        if not sid:
            return events, meta
        kids = con.execute("select count(*) from session where parent_id = ?", (sid,)).fetchone()[0]
        meta.update(child_records=kids, child_detail=bool(kids))
        roles = {r[0]: json.loads(r[1]).get("role") for r in
                 con.execute("select id, data from message where session_id = ? order by time_created", (sid,))}
        for mid, data, created in con.execute(
                "select message_id, data, time_created from part where session_id = ? order by time_created", (sid,)):
            p = json.loads(data)
            t = p.get("type")
            role = roles.get(mid)
            if t == "text" and role in ("user", "assistant"):
                events.append(_ev(role, p.get("text"), created))
            elif t == "compaction":
                events.append(_ev("compaction", p.get("summary") or "context compacted", created))
            elif t == "tool":
                st = p.get("state") or {}
                kind = "child_dispatch" if str(p.get("tool")).lower() in _CHILD_TOOLS else "tool_call"
                events.append(_ev(kind, (st.get("input") or {}).get("command") or st.get("input"), created, tool=p.get("tool")))
                if st.get("status") in ("completed", "error"):
                    events.append(_ev("tool_result", st.get("output") or st.get("error"), created,
                                      tool=p.get("tool"), ok=st.get("status") == "completed"))
    except (sqlite3.Error, ValueError):
        pass
    finally:
        con.close()
    return events, meta


def _record_cwd(res: Resolution) -> str | None:
    """Working directory recorded in the session itself (never the caller's own cwd)."""
    try:
        if res.agent == "opencode":
            con = sqlite3.connect(f"file:{res.path}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute("select directory from session where id = ?", (res.session_id,)).fetchone()
            finally:
                con.close()
            return row[0] if row and isinstance(row[0], str) else None
        for i, d in enumerate(_jsonl(res.path)):
            p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            value = d.get("cwd") or p.get("cwd")
            if isinstance(value, str) and value:
                return value
            if i >= 200:
                break
    except (OSError, sqlite3.Error, ValueError):
        pass
    return None


# --- public entry -----------------------------------------------------------

def read_history(res: Resolution, *, home: Path | None = None, max_events: int = 400) -> dict:
    home = home or Path.home()
    gaps: list[str] = []
    events: list[dict] = []
    meta: dict[str, Any] = {"child_records": 0, "child_detail": False}
    if not res.path or not res.agent:
        gaps.append(res.note or "no readable session record")
    else:
        if res.agent == "claude":
            events, meta = _claude(res.path)
        elif res.agent == "pi":
            events, meta = _pi(res.path)
        elif res.agent == "codex":
            events, meta = _codex(res.path)
        else:
            events, meta = _opencode(res.path, res.session_id if res.method != "newest-in-cwd (heuristic)" else None)
    if res.method.startswith("newest-in-cwd"):
        gaps.append("session chosen by directory heuristic, not by id; confirm it is the active session")
    if len(res.candidates) > 1:
        gaps.append(f"{len(res.candidates)} candidate records in this directory")
    compactions = [i for i, e in enumerate(events) if e["kind"] == "compaction"]
    if compactions and not any(e["kind"] in ("user", "assistant") for e in events[: compactions[0]]):
        gaps.append("turns before compaction are not in the record; only the compaction summary remains")
    dispatches = sum(1 for e in events if e["kind"] == "child_dispatch")
    if (dispatches or meta["child_records"]) and not meta["child_detail"]:
        gaps.append("child-agent activity is visible only as dispatches/final messages, not their own records")
    if not events and not gaps:
        gaps.append("session record contains no turns")
    verdict = "limited" if (not events or res.method.startswith(("newest", "none"))) else (
        "partial" if gaps else "complete")
    omitted = max(0, len(events) - max_events)
    shown = events if not omitted else events[:60] + events[-(max_events - 60):]
    return {
        "agent": res.agent, "session_id": res.session_id, "record": res.path, "method": res.method,
        "cwd": _record_cwd(res) if res.path and res.agent else None,
        "events_total": len(events), "events_omitted_from_middle": omitted, "events": shown,
        "compactions": len(compactions), "child_records": meta["child_records"],
        "coverage": {"verdict": verdict, "gaps": gaps},
        "read_only": True,
    }


def journal_projection(session_id: str | None, state_dir: Path | None = None) -> dict | None:
    """Herdr's own Started/Completed/Current/Next projection, read only if a journal exists."""
    state = state_dir or Path(os.environ.get(
        "HERDR_ENGINEERING_STATE", str(Path.home() / ".local/share/herdr-engineering/state")))
    path = state / "journal.jsonl"
    if not session_id or not path.is_file():
        return None
    from .journal import SessionJournal
    try:
        return SessionJournal(path).summary(session_id).to_dict()
    except Exception:
        return None
