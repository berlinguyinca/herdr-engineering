"""Learning ledger: which proposals may run without asking, and the evidence for that.

Policy is operator-owned YAML (``project_intelligence.auto_actions: {ci_rerun: [owner/repo]}``); the ledger never
widens it. It only (a) counts how each action fared per repository, (b) suggests a promotion once an action has a
clean record, and (c) trips an auto action back to "ask" on the first failure, until the server restarts and an
operator looks. Only low-risk actions can ever be auto; the rest are refused by name.
"""
from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime

AUTO_ELIGIBLE = frozenset({"ci_rerun"})
NEVER_AUTO = frozenset({"merge", "merge_pr", "send_message", "run_shell", "propose_issue", "link_entities",
                        "pause_ingestion", "request_ci_repair"})
PROMOTE_AFTER = 5            # consecutive clean approvals before a promotion is suggested
MAX_AUTO_PER_HOUR = 3        # per action+repo, whatever the policy says


class Ledger:
    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._clock, self._lock = clock, threading.Lock()
        self._stats: dict[tuple[str, str], dict] = {}
        self._tripped: dict[tuple[str, str], str] = {}
        self._auto_times: dict[tuple[str, str], deque] = {}

    def _s(self, key: tuple[str, str]) -> dict:
        return self._stats.setdefault(key, {"approved_clean": 0, "failed": 0, "rejected": 0, "auto_runs": 0})

    @staticmethod
    def policy(section: dict, action: str, repo: str) -> str:
        """``auto`` only when YAML lists this exact repo for an eligible action; otherwise ``ask``."""
        raw = section.get("auto_actions") if isinstance(section, dict) else None
        repos = raw.get(action) if isinstance(raw, dict) else None
        if action in AUTO_ELIGIBLE and action not in NEVER_AUTO and isinstance(repos, list) and repo in repos:
            return "auto"
        return "ask"

    def effective(self, section: dict, action: str, repo: str) -> str:
        with self._lock:
            if (action, repo) in self._tripped:
                return "ask"
        return self.policy(section, action, repo)

    def may_auto_now(self, action: str, repo: str) -> bool:
        """Rate limit; counts the run when it returns True."""
        now = self._clock().timestamp()
        with self._lock:
            q = self._auto_times.setdefault((action, repo), deque())
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= MAX_AUTO_PER_HOUR:
                return False
            q.append(now)
            return True

    def record(self, action: str, repo: str, outcome: str, *, auto: bool = False) -> None:
        """outcome: executed | failed | rejected. A failure of an auto action trips it back to ask."""
        key = (action, repo)
        with self._lock:
            s = self._s(key)
            if outcome == "executed":
                s["approved_clean"] += 1
                s["auto_runs"] += 1 if auto else 0
            elif outcome == "failed":
                s["failed"] += 1
                s["approved_clean"] = 0
                if auto:
                    self._tripped[key] = "an automatic run failed"
            elif outcome == "rejected":
                s["rejected"] += 1
                s["approved_clean"] = 0

    def status(self, section: dict) -> dict:
        with self._lock:
            rows = [{"action": a, "repo": r, **s, "policy": self.policy(section, a, r),
                     "tripped": self._tripped.get((a, r)),
                     "suggest_promotion": (a in AUTO_ELIGIBLE and s["approved_clean"] >= PROMOTE_AFTER
                                           and self.policy(section, a, r) == "ask")}
                    for (a, r), s in sorted(self._stats.items())]
        return {"entries": rows, "never_auto": sorted(NEVER_AUTO), "promote_after": PROMOTE_AFTER}
