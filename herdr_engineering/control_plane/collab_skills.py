"""Skills from the room (operator decision 2026-10-25): `@gert.pi /skill:review src/app.js`.

pi expands `/skill:<name> <args>` only when that text STARTS its prompt, and the worker normally prefixes every task
with "Task from a room member: " (so a task can never be read as a flag, a file reference or a command). The SERVER
decides when a task or a waking mention is a skill invocation and marks it `skill: {name}`; the worker obeys only
that mark, never the text. The mark is set only when:

- YAML ``control_plane.collab.skills.enabled`` is true (default false; re-read per request),
- the sender may give that agent work (``create_task`` / the mention wake rules, checked by the callers),
- the agent is a pi agent (Claude Code runs text-only with slash commands disabled: unchanged),
- the agent REPORTED ``skill:<name>`` at registration (pi's literal form, the one the `/` menu inserts),
- the text, after ONE leading ``@<that agent's address|name|old name>`` (the worker's own strip rule), is
  ``/skill:<name>`` with the name ``[a-z0-9][a-z0-9._-]{0,63}``, then nothing or whitespace and at most
  SKILL_ARGS_MAX characters of arguments.

Each marked task or mention is also a ``skill_invoked`` room event (who, which agent, which skill and metadata about
the arguments). Argument content is never persisted in the audit event. Delivery re-checks the switch, the harness
and the reported list, so turning the switch off takes effect at once.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
SKILL_ARGS_MAX = 2000
# the worker's rule (packages/collab-scripts.js): ONE leading "@<own name>" plus whitespace, only if text follows
_LEADING = re.compile(r"@([A-Za-z0-9._-]{1,48})\s+(?=\S)")
_INVOKE = re.compile(r"/skill:([^\s]*)(?:\s+(.*))?\Z", re.S)


def normalize_skills(value: Any) -> dict:
    """``control_plane.collab.skills`` -> {enabled}. Absent = off."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("control_plane.collab.skills must be a mapping")
    unknown = sorted(str(k) for k in set(value) - {"enabled"})
    if unknown:
        raise ValueError("control_plane.collab.skills: unknown keys: " + ", ".join(unknown))
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("control_plane.collab.skills.enabled must be true or false")
    return {"enabled": enabled}


def parse_skill(text: str, own: set[str]) -> tuple[str, str] | None:
    """(name, args) when ``text`` (after one leading own address) invokes a skill, else None."""
    if not isinstance(text, str):
        return None
    m = _LEADING.match(text)
    if m:
        if m.group(1).lower() not in own:
            return None
        text = text[m.end():]
    inv = _INVOKE.match(text)
    if not inv or not SKILL_NAME.match(inv.group(1)):
        return None
    args = (inv.group(2) or "").strip()
    if len(args) > SKILL_ARGS_MAX:
        return None
    return inv.group(1), args


class SkillsMixin:
    """For CollabService: the server-side decision, the mark at delivery and the audit event."""

    def _init_skills(self) -> None:
        self.skills_config = normalize_skills(None)
        self.skills_source = None              # optional callable -> raw YAML mapping, read per request

    def _skills_enabled(self) -> bool:
        if self.skills_source is not None:
            try:
                return normalize_skills(self.skills_source())["enabled"]
            except Exception:                    # a bad edit fails closed
                return False
        return bool((self.skills_config or {}).get("enabled"))

    def _own_names(self, aid: str) -> set[str]:
        a = self._agents.get(aid) or {}
        return {n.lower() for n in [self._address(aid), a.get("name"), *(a.get("aliases") or ())] if n}

    def _skill_call(self, aid: str, text: str) -> tuple[str, str] | None:
        """(name, args) if ``text`` given to agent ``aid`` may run that skill now (the sender's authority is the
        caller's check). Call under the lock."""
        if not self._skills_enabled():
            return None
        a = self._agents.get(aid)
        if not a or a.get("harness_type") != "pi" or a.get("external"):     # a remote agent's label is its own claim
            return None
        call = parse_skill(text, self._own_names(aid))
        if call is None or f"skill:{call[0]}" not in (a.get("skills") or ()):
            return None
        return call

    def _skill_mark(self, aid: str, text: str, stored: Any) -> dict | None:
        """The delivery mark: the stored decision, still true now."""
        if not isinstance(stored, str):
            return None
        call = self._skill_call(aid, text)
        return {"name": stored} if call and call[0] == stored else None

    def _skill_audit(self, pr, room: dict, room_id: str, aid: str, call: tuple[str, str], **ref) -> None:
        args = call[1].encode()
        self._commit("skill_invoked", self._actor(
            pr, project_id=room["project_id"], room_id=room_id, agent_id=aid, skill=call[0],
            args_bytes=len(args), args_sha256=hashlib.sha256(args).hexdigest(),
            by_kind=pr.kind, by_id=pr.id, **ref, visibility_scope="room", action="skill.invoke",
            outcome="allowed", project_ids=[room["project_id"]], resource_id=aid))
