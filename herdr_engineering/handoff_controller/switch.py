"""Auto-handoff on/off: globally via ``HERDR_HANDOFF_AUTO=0``, per pane via a marker file."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..handoff_subagent_registry import sanitize


def _marker(pane_id: str, state_dir: Path) -> Path:
    return state_dir / "handoff-controller" / "off" / sanitize(pane_id)


def _state(pane_id: str, state_dir: Path) -> str | None:
    try:
        return _marker(pane_id, state_dir).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return "off"  # a marker that cannot be read is not permission to act


def is_enabled(pane_id: str, env: Mapping[str, str], state_dir: Path) -> bool:
    """Off globally, or per pane when its marker says `off` (an empty marker, written by an older version, does too)."""
    return env.get("HERDR_HANDOFF_AUTO") != "0" and _state(pane_id, state_dir) in (None, "on")


def set_enabled(pane_id: str, enabled: bool, state_dir: Path) -> None:
    """The marker stays when the switch is turned on again, saying `on`: its mtime is when the switch was last
    toggled, which the controller reads to start over even when it never saw the switch off."""
    marker = _marker(pane_id, state_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("on\n" if enabled else "off\n", encoding="utf-8")


def toggled_at(pane_id: str, state_dir: Path) -> float | None:
    """When the switch of this pane was last turned on or off (wall clock), None when it never was."""
    try:
        return _marker(pane_id, state_dir).stat().st_mtime
    except OSError:
        return None
