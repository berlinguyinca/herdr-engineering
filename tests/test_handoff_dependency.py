from pathlib import Path

import pytest

from herdr_engineering.handoff_dependency import CLI_COMMANDS, HARNESS_DIRS, \
    check_handoff_dependency


def test_handoff_dependency_accepts_a_complete_install(tmp_path: Path):
    result = check_handoff_dependency(skill_root=_skills_tree(tmp_path / "skills"),
                                     executable=_fake_cli(tmp_path, version="0.3.0", commands=CLI_COMMANDS))
    assert result.status == "ok", result.detail
    assert result.missing_harnesses == []
    assert result.version == "0.3.0"


def test_handoff_dependency_names_the_harness_missing_a_skill(tmp_path: Path):
    tree = _skills_tree(tmp_path / "skills")
    (tree / "pi" / "herdr-handoff" / "SKILL.md").unlink()
    result = check_handoff_dependency(skill_root=tree,
                                     executable=_fake_cli(tmp_path, version="0.3.0", commands=CLI_COMMANDS))
    assert result.status == "fail" and "pi:herdr-handoff" in result.detail
    assert result.version == "0.3.0"   # the skills are meant to match this CLI, so say which one it is


def test_strict_dependency_fails_when_cli_or_skill_is_missing(tmp_path: Path):
    result = check_handoff_dependency(skill_root=tmp_path / "missing", executable=None)
    assert result.status == "fail"
    assert "herdr-handoff" in result.detail


def _skills_tree(root: Path) -> Path:
    for harness in HARNESS_DIRS:
        for skill in ("herdr", "herdr-handoff", "herdr-artifact"):
            directory = root / harness / skill
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text(f"# {skill}\n")
    return root


def _fake_cli(tmp_path: Path, *, version: str, commands: tuple[str, ...],
              style: str = "argparse") -> str:
    """A real executable that answers --version/--help the way a herdr-handoff release does.

    `argparse` is the shape the shipped CLI actually prints (one brace group on the usage line);
    `listed` is a hand-written help that spells the commands out one per line, so both spellings get
    exercised.
    """
    path = tmp_path / f"herdr-handoff-{version}-{style}"
    choices = ",".join(commands)
    if style == "argparse":
        help_text = (f"usage: herdr-handoff [-h] [--version] [--root ROOT]\n"
                     f"                     {{{choices}}} ...\n"
                     "\npositional arguments:\n"
                     f"  {{{choices}}}\n"
                     "\noptions:\n"
                     "  -h, --help            show this help message and exit\n")
    else:
        help_text = "usage: herdr-handoff <command>\n\ncommands:\n" + "".join(
            f"  {command} [--id X]   description\n" for command in commands)
    path.write_text("#!/usr/bin/env python3\nimport sys\n"
                    f"HELP = {help_text!r}\n"
                    f"if \"--version\" in sys.argv: print({version!r})\n"
                    "elif \"--help\" in sys.argv: print(HELP, end=\"\"), sys.exit(0)\n")
    path.chmod(0o755)
    return str(path)


# What `herdr-eng` invokes; the real 0.3.0 CLI offers
# {create,inspect,list,transition,resume,sync,launch,takeover,confirm}.
REQUIRED = ("create", "launch", "takeover", "confirm")


def test_the_cli_is_checked_for_the_commands_we_actually_call(tmp_path):
    # An existence check is not proof that a flow we depend on is reachable.
    tree = _skills_tree(tmp_path)
    result = check_handoff_dependency(skill_root=tree, required_commands=REQUIRED,
                                     executable=_fake_cli(tmp_path, version="0.3.0", commands=REQUIRED))
    assert result.status == "ok", result.detail
    assert result.version == "0.3.0"
    assert set(result.capabilities) == set(REQUIRED)
    assert all(result.capabilities.values())


def test_a_cli_without_takeover_is_reported_as_the_gap_it_is(tmp_path):
    # takeover and confirm are v0.3.0; a host pinned to v0.2.0 passes an existence check and cannot
    # honour the close a takeover plan asks for.
    tree = _skills_tree(tmp_path)
    result = check_handoff_dependency(skill_root=tree, required_commands=REQUIRED,
                                     executable=_fake_cli(tmp_path, version="0.2.0",
                                                          commands=("create", "launch")))
    assert result.status == "fail"
    assert "takeover" in result.detail and "confirm" in result.detail
    assert "0.2.0" in result.detail
    assert result.capabilities["takeover"] is False


def test_the_capability_probe_does_not_invent_commands(tmp_path):
    tree = _skills_tree(tmp_path)
    result = check_handoff_dependency(skill_root=tree, required_commands=(),
                                     executable=_fake_cli(tmp_path, version="0.3.0", commands=("create",)))
    assert result.status == "ok"       # nothing was required of it
    assert result.capabilities["takeover"] is False


def test_a_cli_that_cannot_be_run_reports_no_capabilities(tmp_path):
    tree = _skills_tree(tmp_path)
    broken = tmp_path / "broken-handoff"
    broken.write_text("#!/nonexistent/interpreter\n")
    broken.chmod(0o755)
    result = check_handoff_dependency(skill_root=tree, required_commands=("takeover",), executable=str(broken))
    assert result.status == "fail"
    assert result.capabilities == {}
    assert "could not be run" in result.detail

