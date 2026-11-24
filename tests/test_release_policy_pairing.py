"""The old context policies and the controller ship together: removing one without the other leaves a session with no policy
(or two policies typing into the same session). Reads the real files of this checkout."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from herdr_engineering import workflow_hooks

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "herdr_engineering/skill_data/handoff/SKILL.md").read_text()
TRIGGERS = (ROOT / "docs/operations/handoff-auto-triggers.md").read_text()
HANDOFF_DOC = (ROOT / "docs/operations/handoff.md").read_text()
CHANGELOG = (ROOT / "CHANGELOG.md").read_text()


def test_controller_plugin_present_when_old_hooks_removed() -> None:
    assert (ROOT / "deploy/herdr-handoff-controller-plugin/herdr-plugin.toml").is_file()
    assert importlib.util.find_spec("herdr_engineering.handoff_context_hook") is None


def test_nothing_tells_the_agent_to_hand_off_because_of_context_size() -> None:
    assert "handoff" not in workflow_hooks.REMINDER.lower()
    assert not hasattr(workflow_hooks, "PRECOMPACT_MESSAGE")
    assert "context-hook" not in SKILL
    for line in SKILL.splitlines():
        assert not re.search(r"(context|50\s?%|80\s?%).*(run|use) `?/handoff|/handoff.*(because|when).*(context|full)", line, re.I), line
    assert "/handoff [pi|claude|codex|opencode]" in SKILL  # the skill itself stays: people still hand off by hand


def test_skill_and_docs_require_herdr_handoff_0_5_0() -> None:
    assert "must print `0.5.0` or newer" in SKILL
    assert "needs\n  herdr-handoff 0.5.0 or newer" in HANDOFF_DOC
    assert "0.4.0 or newer" not in SKILL + HANDOFF_DOC


def test_triggers_doc_describes_one_policy_and_the_upgrade() -> None:
    assert "## Upgrading" in TRIGGERS
    upgrading = TRIGGERS.split("## Upgrading", 1)[1].split("\n## ", 1)[0]
    for needle in ("herdr-eng handoff auto install", "herdr-eng workflow install", "silent no-op",
                   "AUTOSPEC_CONTEXT_MONITOR_SKIP_HERDR", "HERDR_PANE_ID", "no automatic policy"):
        assert needle in upgrading, needle
    assert "plan 5" not in TRIGGERS and "plan 5" not in HANDOFF_DOC
    assert "Until plan 5 ships" not in HANDOFF_DOC


def test_changelog_records_the_retirement() -> None:
    unreleased = CHANGELOG.split("\n## [", 2)[1]
    assert "handoff context-hook" in unreleased and "PreCompact" in unreleased and "AUTOSPEC_CONTEXT_MONITOR_SKIP_HERDR" in unreleased
    assert "retired by plan 5" not in unreleased
