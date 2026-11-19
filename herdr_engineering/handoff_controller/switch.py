"""Auto-handoff on/off: globally via ``HERDR_HANDOFF_AUTO=0``, per pane via a marker file."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..handoff_subagent_registry import sanitize


def _marker(pane_id: str, state_dir: Path) -> Path:
    return state_dir / "handoff-controller" / "off" / sanitize(pane_id)


def is_enabled(pane_id: str, env: Mapping[str, str], state_dir: Path) -> bool:
    return env.get("HERDR_HANDOFF_AUTO") != "0" and not _marker(pane_id, state_dir).exists()


def set_enabled(pane_id: str, enabled: bool, state_dir: Path) -> None:
    marker = _marker(pane_id, state_dir)
    if enabled:
        marker.unlink(missing_ok=True)
    else:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
