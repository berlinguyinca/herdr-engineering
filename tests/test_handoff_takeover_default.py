"""The pin, the shipped skill and the ops doc must agree on what a handoff now does.

`/handoff` composes a briefing and then calls the `herdr-handoff` CLI, so the skill text *is* the
behaviour an agent performs: while it says `launch`, an agent leaves the replaced session running beside
its successor no matter which revision the pin names.  herdr-handoff 0.4.0 (herdr-handoff#7) made
handover the default for `/herdr::to*`, added `--keep-source` as the keep-both escape hatch, and added
the `source_dirty` guard that refuses to close a pane still holding uncommitted or unpushed work.  These
tests keep the three written surfaces honest about that one behaviour.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from herdr_engineering.handoff_dependency import CLI_COMMANDS

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "herdr_engineering" / "skill_data" / "handoff" / "SKILL.md"
OPS_DOC = ROOT / "docs" / "operations" / "handoff.md"
LOCK = ROOT / "lock" / "upstreams.yaml"

REFUSALS = ("launch_unconfirmed", "launch_failed", "source_close_failed", "source_dirty")


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def _doc() -> str:
    return OPS_DOC.read_text(encoding="utf-8")


def test_the_lock_pin_names_the_revision_that_hands_off_by_default():
    entry = yaml.safe_load(LOCK.read_text(encoding="utf-8"))["dependencies"]["herdr-handoff"]
    assert re.fullmatch(r"[0-9a-f]{40}", entry["revision"]), "the pin must be a full commit sha"
    note = entry["note"]
    assert "v0.4.0" in note and "herdr-handoff#7" in note, note
    # The guard is part of what the pin buys: a host told only "takeover exists" cannot know a pane may
    # now refuse to close because of the work it holds.
    assert "source_dirty" in note and "--keep-source" in note, note


def test_the_lock_file_offers_one_revision_per_dependency():
    """`scripts/install-handoff.sh` reads the FIRST `revision:` line; PyYAML keeps the LAST.

    Two `revision:` keys under one dependency therefore resolve to two different commits depending on who
    asks - the quiet drift this pin exists to prevent, and easy to introduce by editing the note block.
    """
    block = re.search(
        r"^  herdr-handoff:\n((?:    \S.*\n)+)", LOCK.read_text(encoding="utf-8"), re.M
    )
    assert block, "herdr-handoff is not pinned in lock/upstreams.yaml"
    keys = [re.match(r"    ([A-Za-z_-]+):", line).group(1) for line in block.group(1).splitlines()]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert not duplicates, f"duplicate keys under herdr-handoff: {duplicates}"


def test_the_skill_hands_off_with_takeover_instead_of_leaving_the_source_running():
    text = _skill()
    assert "herdr-handoff takeover" in text, "the handover step must be takeover, not launch"
    assert "herdr-handoff launch <handoff_id>" not in text, "launch is no longer the default step"
    for token in ("--keep-source", "source_dirty", "--allow-dirty-source"):
        assert token in text, f"the skill omits {token}"


def test_the_skill_puts_the_destructive_step_last():
    """The rule that keeps a handoff from losing work: durable first, close last, and never a retry of a refusal."""
    text = _skill().lower()
    assert "durable" in text, "the skill must say the work has to be durable before the close"
    assert "by hand" in text, "the skill must forbid closing a pane by hand to get around a refusal"
    assert "never a retry" in text, "--allow-dirty-source must not read as a way to retry a refusal"


def test_every_cli_command_the_skill_invokes_is_covered_by_the_dependency_probe():
    """`herdr-eng doctor` credits a host with capabilities it may not have if the probe lags the skill.

    Scoped to the skill, not the ops doc: the doc describes operator commands (`sync`, `inspect`) that no
    agent runs, while every subcommand in the skill is one an agent will type.
    """
    invoked = {m for m in re.findall(r"`herdr-handoff ([a-z][a-z-]+)", _skill())}
    unprobed = sorted(command for command in invoked if command not in CLI_COMMANDS)
    assert not unprobed, f"the skill calls commands the probe never checks: {unprobed}"
    assert "takeover" in invoked, "the skill no longer instructs a takeover at all"


def test_the_ops_doc_documents_the_default_and_every_refusal():
    text = _doc()
    assert "`herdr-handoff takeover`" in text, "the doc must say what /herdr::to* runs now"
    for status in REFUSALS:
        assert status in text, f"the doc omits the {status} refusal"
    assert "--keep-source" in text


def test_the_ops_doc_does_not_keep_the_stale_pin_note():
    assert "still names the revision without" not in _doc()


# Interfaces herdr-handoff 0.4.0 does NOT offer.  Each was once written here: `confirm` takes only
# `--pane`, `takeover` has no `--no-confirm` (the wait is `--confirm-timeout`), and an unreadable worktree
# surfaces as `source_dirty` with a `worktree_unknown` reason rather than as its own status.  A skill that
# names a flag which does not exist tells the agent to type something that will fail, mid-handoff.
PHANTOM_INTERFACES = ("--close-source", "--no-confirm", "status: worktree_unknown")


def test_the_shipped_text_promises_no_interface_the_cli_does_not_have():
    text = _skill() + _doc()
    for phantom in PHANTOM_INTERFACES:
        assert phantom not in text, f"{phantom} is not part of the herdr-handoff takeover interface"


def test_flags_the_skill_types_for_takeover_exist_in_the_installed_cli():
    """Against the CLI on PATH, because argparse rejects an unknown flag with exit 2."""
    cli = shutil.which("herdr-handoff")
    if cli is None:
        pytest.skip("herdr-handoff CLI is not installed here")
    shown = subprocess.run([cli, "takeover", "--help"], capture_output=True, text=True, timeout=30)
    assert shown.returncode == 0, shown.stderr
    advertised = set(re.findall(r"--[a-z][a-z-]+", shown.stdout + shown.stderr))
    for surface, text in (("skill", _skill()), ("ops doc", _doc())):
        for line in text.splitlines():
            if "herdr-handoff takeover" not in line:
                continue
            # Flags inside a shell substitution belong to whatever command is in there (`git rev-parse
            # --show-toplevel`), not to herdr-handoff.
            line = re.sub(r"\$\([^)]*\)", "", line)
            unknown = sorted(
                flag for flag in re.findall(r"--[a-z][a-z-]+", line) if flag not in advertised
            )
            assert not unknown, (
                f"the {surface} types {unknown}; takeover offers {sorted(advertised)}"
            )


def test_the_skill_finishes_or_hands_over_running_subagents_before_the_close():
    """Closing the pane kills this session's subagents; the skill must not close under running work."""
    text = _skill().lower()
    assert "subagent" in text, "the skill must address subagents that are still running"
    assert "before the close" in text or "before running `herdr-handoff takeover`" in text
    assert "--keep-source" in text and "still running" in text, (
        "the skill must say to keep the source when work is still running"
    )
