"""The conversation id Herdr reports for a pane's agent, shared by the adapters that match records by id."""
import json
import shutil
import subprocess
from collections.abc import Mapping


def herdr_session_id(pane_id: str, env: Mapping[str, str] | None) -> str | None:
    """The conversation id Herdr reports for the pane's agent (``herdr agent get``), or None."""
    exe = shutil.which("herdr", path=None if env is None else env.get("PATH", ""))
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "agent", "get", pane_id], capture_output=True, text=True, timeout=10,
                             env=None if env is None else dict(env))
        session = json.loads(out.stdout)["result"]["agent"].get("agent_session")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return None
    value = session.get("value") if isinstance(session, dict) and session.get("kind") == "id" else None
    return value if isinstance(value, str) and value else None


def is_safe_session_id(session_id: str | None) -> bool:
    """True for an id that is safe to put in a path or glob: no separators, no `..`, no glob metacharacters."""
    if not session_id or session_id == "..":
        return False
    return not any(c in session_id for c in "/\\*?[")
