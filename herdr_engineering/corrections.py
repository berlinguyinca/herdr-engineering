"""A small, opt-in record of when you corrected an agent, across Claude Code, Codex, Pi and OpenCode.

It only measures. Nothing here changes an agent, a prompt, a memory or a routing rule; a later step may turn repeated
corrections into proposals you approve. Design points:

* Pull, not push. Corrections are read afterwards from the session records ``session_history`` already reads (read-only,
  redacted, clipped), so nothing sits in an agent's request path and there is no added latency.
* Off until ``enable``. ``scan`` refuses while disabled; ``disable`` stops it; ``forget`` deletes what was kept.
* Human turns only. Turns the harness or another agent injected (gates, task notifications, agent messages) are ignored.
* Deterministic. Four kinds, found by fixed patterns, never by a model reading your chats.
* Small. A record holds the harness, the session, a short excerpt of what you said and of what you were answering. It is
  not a transcript, and it never holds hidden reasoning.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

KINDS = ("negation", "reask", "interrupt", "rejected_tool")
EXCERPT = 160
CONTEXT = 120
REASK_WINDOW = 12            # user turns are compared against the last few events only
REASK_SIMILARITY = 0.6
MIN_REASK_WORDS = 4

_NEGATION = re.compile(
    r"^\W*(no\b|nope\b|not (that|what)\b|that'?s (wrong|not)\b|wrong\b|don'?t\b|do not\b|stop\b|instead\b|"
    r"actually\b|undo\b|revert\b|i (said|meant|asked)\b)", re.IGNORECASE)
_INTERRUPT = re.compile(r"\[?request interrupted by user|interrupted by user|turn aborted", re.IGNORECASE)
_REJECTED = re.compile(r"doesn'?t want to proceed|user (rejected|denied|declined)|tool use was rejected|permission denied by user",
                       re.IGNORECASE)
# Turns that arrive in the user role but were not typed by you: harness gates, task notifications, agent-to-agent messages.
_AUTOMATED = re.compile(r"^\s*(<|\[harness\]|another \w+ session sent a message|this session is being continued|"
                        r"[\w-]+ hook (feedback|blocked|error))",
                        re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True)
class Correction:
    id: str
    kind: str
    agent: str
    session_id: str
    index: int
    ts: Any
    excerpt: str
    context: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if len(wa) < MIN_REASK_WORDS or len(wb) < MIN_REASK_WORDS:
        return False
    return len(wa & wb) / len(wa | wb) >= REASK_SIMILARITY


def _make(kind: str, agent: str, session_id: str, index: int, event: dict, context: str) -> Correction:
    key = f"{agent}|{session_id}|{index}|{kind}"
    return Correction(hashlib.sha256(key.encode()).hexdigest()[:20], kind, agent, session_id, index, event.get("ts"),
                      str(event.get("text", ""))[:EXCERPT], context[:CONTEXT])


def extract(events: Iterable[dict], *, agent: str, session_id: str) -> list[Correction]:
    """Corrections in one session's normalized events (``session_history`` shape). Pure and idempotent: the same events
    always give the same ids, so scanning a session twice records nothing new."""
    evs = list(events)
    found: list[Correction] = []
    last_agent_text = ""
    seen_user: list[tuple[int, str]] = []
    for i, e in enumerate(evs):
        kind, text = e.get("kind"), str(e.get("text", ""))
        if kind == "assistant":
            last_agent_text = text
        elif kind == "tool_result" and e.get("ok") is False and _REJECTED.search(text):
            found.append(_make("rejected_tool", agent, session_id, i, e, f"{e.get('tool') or 'tool'}: {last_agent_text}"))
        elif kind == "user":
            if _AUTOMATED.search(text) or (seen_user and seen_user[-1][1] == text):
                continue                      # not typed by a person, or the same turn logged twice
            if _INTERRUPT.search(text):
                found.append(_make("interrupt", agent, session_id, i, e, last_agent_text))
                continue
            if last_agent_text and _NEGATION.search(text):
                found.append(_make("negation", agent, session_id, i, e, last_agent_text))
            elif any(i - j <= REASK_WINDOW and _similar(text, earlier) for j, earlier in seen_user):
                found.append(_make("reask", agent, session_id, i, e, last_agent_text))
            seen_user.append((i, text))
    return found


class CorrectionLog:
    """Append-only JSONL beside an enable marker, both private to the user. One process writes at a time."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.path = self.dir / "corrections.jsonl"
        self.marker = self.dir / "enabled"
        self._lock = threading.Lock()

    @classmethod
    def default(cls) -> CorrectionLog:
        state = os.environ.get("HERDR_ENGINEERING_STATE", str(Path.home() / ".local/share/herdr-engineering/state"))
        return cls(Path(state) / "corrections")

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def enabled(self) -> bool:
        return self.marker.is_file()

    def enable(self) -> None:
        self._ensure()
        self.marker.write_text("on\n")
        os.chmod(self.marker, 0o600)

    def disable(self) -> None:
        self.marker.unlink(missing_ok=True)

    def read(self, *, agent: str | None = None, kind: str | None = None, session_id: str | None = None) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and (agent is None or row.get("agent") == agent) \
                    and (kind is None or row.get("kind") == kind) \
                    and (session_id is None or row.get("session_id") == session_id):
                out.append(row)
        return out

    def record(self, corrections: Iterable[Correction]) -> int:
        """Appends the ones not already kept; returns how many were new. Refuses while disabled."""
        if not self.enabled():
            raise PermissionError("corrections are off; run `herdr-eng corrections enable` first")
        with self._lock:
            self._ensure()
            have = {r.get("id") for r in self.read()}
            fresh = [c for c in corrections if c.id not in have]
            if fresh:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a") as handle:
                    for c in fresh:
                        handle.write(json.dumps(c.as_dict(), sort_keys=True) + "\n")
            return len(fresh)

    def forget(self, *, session_id: str | None = None, everything: bool = False) -> int:
        """Deletes one session's records, or with ``everything=True`` all of them; returns how many were removed.
        Naming neither is an error, so a missing argument can never wipe the log. Works while disabled."""
        if session_id is None and not everything:
            raise ValueError("name a session or pass everything=True")
        with self._lock:
            rows = self.read()
            keep = [r for r in rows if not everything and r.get("session_id") != session_id]
            if len(keep) == len(rows):
                return 0
            if not keep:
                self.path.unlink(missing_ok=True)
            else:
                fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".corrections-")
                with os.fdopen(fd, "w") as handle:
                    handle.writelines(json.dumps(r, sort_keys=True) + "\n" for r in keep)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            return len(rows) - len(keep)

    def stats(self) -> dict[str, Any]:
        rows = self.read()
        return {"enabled": self.enabled(), "total": len(rows),
                "by_agent": dict(sorted(Counter(r.get("agent", "?") for r in rows).items())),
                "by_kind": dict(sorted(Counter(r.get("kind", "?") for r in rows).items())),
                "sessions": len({r.get("session_id") for r in rows})}


def scan_session(log: CorrectionLog, *, agent: str | None = None, session: str | None = None, cwd: str | None = None,
                 home: Path | None = None) -> dict[str, Any]:
    """Reads one session through ``session_history`` (read-only) and records its corrections."""
    from . import session_history as sh
    res = sh.resolve(agent=agent, session=session, cwd=cwd, home=home)
    history = sh.read_history(res, home=home, max_events=100000)
    sid = res.session_id or (Path(res.path).stem if res.path else "")
    found = extract(history["events"], agent=history["agent"] or "", session_id=sid) if history["agent"] and sid else []
    return {"agent": history["agent"], "session_id": sid, "found": len(found), "new": log.record(found),
            "coverage": history["coverage"]}


def scan_recent(log: CorrectionLog, *, agents: Iterable[str] = ("claude", "codex", "pi"), limit: int = 20,
                home: Path | None = None) -> dict[str, Any]:
    """Scans each harness's newest ``limit`` session records (by modification time). Read-only; one unreadable record is
    reported and skipped. OpenCode keeps one database rather than a file per session, so it is scanned by id instead."""
    import glob

    from . import session_history as sh
    home = home or Path.home()
    out: dict[str, Any] = {"scanned": 0, "found": 0, "new": 0, "skipped": []}
    for agent in agents:
        store = sh._stores(home).get(agent)
        if store is None or agent == "opencode":
            continue
        files = [f for f in glob.glob(str(store / "**" / "*.jsonl"), recursive=True) if "subagents" not in f]
        for path in sorted(files, key=os.path.getmtime, reverse=True)[:max(0, limit)]:
            try:
                res = scan_session(log, agent=agent, session=path, home=home)
            except PermissionError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad record must not stop the rest
                out["skipped"].append({"agent": agent, "record": Path(path).name, "reason": type(exc).__name__})
                continue
            out["scanned"] += 1
            out["found"] += res["found"]
            out["new"] += res["new"]
    return out
