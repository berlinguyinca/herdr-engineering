"""Deploy matching: the hub (not the worker) decides project and scope, and only for one exact argv form."""
from typing import Any

import pytest

from herdr_engineering.control_plane.assistant import assistant_config
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, argv_digest, derive
from herdr_engineering.control_plane.assistant_hub.deploy import (
    DeployMatch,
    match_deploy,
    scope_key,
)
from herdr_engineering.control_plane.assistant_hub.verbs import Verb
from herdr_engineering.control_plane.assistant_hub.vetoes import check_vetoes

CMD = "deploy/control-plane/deploy.sh"
REPO = "berlinguyinca/herdr-engineering"
SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "fedcba9876543210fedcba9876543210fedcba98"
CFG = assistant_config({"personal_assistant": {"deploy": {"projects": {"herdr-engineering": {
    "repo": REPO, "scopes": {"control-plane": {"command": CMD, "mode": "auto"},
                             "other": {"command": "deploy/other/run.sh"}}}}}}})["deploy"]


def raw(command: str | None = f"{CMD} --ref {SHA}", *, tool: str = "Bash", repo: str | None = REPO, **kw: Any) -> RawAction:
    return RawAction(tool=tool, command=command, repo=repo, **kw)


def test_exact_form_matches_and_yields_a_sha_bound_key() -> None:
    m = match_deploy(raw(), CFG)
    argv = [CMD, "--ref", SHA]
    assert m == DeployMatch("herdr-engineering", "control-plane", REPO, SHA, argv_digest(argv))
    d = derive(raw(), CFG)
    assert d.deploy == m and d.key is not None
    assert (d.key.tool, d.key.verb, d.key.resource_scope, d.key.target) == ("deploy", Verb.DEPLOY_PROD, REPO,
                                                                             argv_digest(argv))
    assert d.owner_decides is False and d.one_shot_safe is True and d.elements == (d.key,)
    assert check_vetoes(d) is None                                  # deploy.sh is not newly vetoed


def test_each_sha_is_its_own_key_but_the_scope_key_is_shared() -> None:
    a, b = derive(raw(), CFG), derive(raw(f"{CMD} --ref {SHA2}"), CFG)
    assert a.key.key_id() != b.key.key_id()
    ma, mb = a.deploy, b.deploy
    assert scope_key(ma) == scope_key(mb) and scope_key(ma).key_id() != a.key.key_id()
    assert (scope_key(ma).verb, scope_key(ma).resource_scope) == (Verb.DEPLOY_PROD, REPO)
    assert "control-plane" in scope_key(ma).target and scope_key(ma).target != ""


def test_the_scope_key_never_collides_with_the_deploy_tool_key() -> None:
    tool = derive(RawAction(tool="deploy", repo=REPO, environment="prod"), CFG)
    assert tool.deploy is None
    assert tool.key.key_id() != scope_key(match_deploy(raw(), CFG)).key_id()


def test_other_scope_and_repo_case_and_accepted_tools() -> None:
    m = match_deploy(raw("deploy/other/run.sh --ref " + SHA), CFG)
    assert m is not None and m.scope == "other"
    assert match_deploy(raw(repo=REPO.upper()), CFG) is not None             # GitHub names are case-insensitive
    for tool in ("shell", "bash", "Bash", "sh", "zsh", "exec", "terminal"):
        assert match_deploy(raw(tool=tool), CFG) is not None, tool


def test_a_hint_that_agrees_still_matches() -> None:
    assert match_deploy(raw(hint={"repo": REPO}), CFG) is not None
    assert match_deploy(raw(hint={"cwd": "/work/checkout"}), CFG) is not None


BAD_COMMANDS = {
    "equals sign": f"{CMD} --ref={SHA}",
    "branch": f"{CMD} --ref main",
    "short sha": f"{CMD} --ref {SHA[:12]}",
    "abbrev 7": f"{CMD} --ref {SHA[:7]}",
    "uppercase": f"{CMD} --ref {SHA.upper()}",
    "mixed case": f"{CMD} --ref {SHA[:20].upper()}{SHA[20:]}",
    "41 chars": f"{CMD} --ref {SHA}0",
    "39 chars": f"{CMD} --ref {SHA[:-1]}",
    "non hex": f"{CMD} --ref {SHA[:-1]}g",
    "unicode digits": f"{CMD} --ref {SHA[:-1]}٣",
    "extra arg after": f"{CMD} --ref {SHA} --dry-run",
    "extra arg after plain": f"{CMD} --ref {SHA} extra",
    "extra arg before": f"{CMD} --verbose --ref {SHA}",
    "extra arg before plain": f"{CMD} x --ref {SHA}",
    "no ref": CMD,
    "ref without sha": f"{CMD} --ref",
    "ref twice": f"{CMD} --ref {SHA} --ref {SHA2}",
    "other script": f"deploy/other/deploy.sh --ref {SHA}",
    "script prefix": f"{CMD}x --ref {SHA}",
    "dot slash": f"./{CMD} --ref {SHA}",
    "absolute": f"/{CMD} --ref {SHA}",
    "absolute checkout": f"/home/x/herdr/{CMD} --ref {SHA}",
    "parent": f"../{CMD} --ref {SHA}",
    "double slash": f"deploy//control-plane/deploy.sh --ref {SHA}",
    "case": f"Deploy/control-plane/deploy.sh --ref {SHA}",
    "and chain": f"{CMD} --ref {SHA} && echo done",
    "and chain before": f"true && {CMD} --ref {SHA}",
    "semicolon": f"{CMD} --ref {SHA}; ls",
    "pipe": f"{CMD} --ref {SHA} | tee log",
    "background": f"{CMD} --ref {SHA} &",
    "or chain": f"{CMD} --ref {SHA} || true",
    "env wrapper": f"env {CMD} --ref {SHA}",
    "env prefix": f"FOO=1 {CMD} --ref {SHA}",
    "ssh wrapper": f"ssh host {CMD} --ref {SHA}",
    "sh -c": f"sh -c '{CMD} --ref {SHA}'",
    "bash": f"bash {CMD} --ref {SHA}",
    "sudo": f"sudo {CMD} --ref {SHA}",
    "nohup": f"nohup {CMD} --ref {SHA}",
    "dynamic sha": f"{CMD} --ref $SHA",
    "dynamic subst": f"{CMD} --ref $(git rev-parse HEAD)",
    "backtick": f"{CMD} --ref `git rev-parse HEAD`",
    "glob": f"{CMD} --ref {SHA[:39]}*",
    "redirect": f"{CMD} --ref {SHA} > /tmp/out",
    "redirect in": f"{CMD} --ref {SHA} < /dev/null",
    "heredoc": f"{CMD} --ref {SHA} <<EOF\nx\nEOF",
    "trailing newline": f"{CMD} --ref {SHA}\n",
    "trailing newline extra": f"{CMD} --ref {SHA}\nrm -rf /",
    "leading newline": f"\n{CMD} --ref {SHA}",
    "trailing space": f"{CMD} --ref {SHA} ",
    "leading space": f" {CMD} --ref {SHA}",
    "double space": f"{CMD}  --ref {SHA}",
    "tab": f"{CMD}\t--ref {SHA}",
    "carriage return": f"{CMD} --ref {SHA}\r",
    "quoted script": f"'{CMD}' --ref {SHA}",
    "quoted sha": f"{CMD} --ref '{SHA}'",
    "double quoted": f'{CMD} --ref "{SHA}"',
    "escaped": f"{CMD} --ref \\{SHA}",
    "nul": f"{CMD} --ref {SHA}\x00",
    "comment": f"{CMD} --ref {SHA} # x",
    "subshell": f"({CMD} --ref {SHA})",
    "brace": f"{{ {CMD} --ref {SHA}; }}",
    "empty": "",
    "only space": "   ",
}


@pytest.mark.parametrize("name", sorted(BAD_COMMANDS))
def test_non_matches_stay_ordinary_unknowns(name: str) -> None:
    command = BAD_COMMANDS[name]
    r = raw(command)
    assert match_deploy(r, CFG) is None
    d = derive(r, CFG)
    assert d.deploy is None
    assert d == derive(r)                                          # derive is unchanged when nothing matched
    assert d.owner_decides is True and d.key is not None and d.key.verb is not Verb.DEPLOY_PROD, name


def test_missing_or_disagreeing_repo_is_no_match() -> None:
    for bad in (None, "", "other/repo", REPO + "x", "berlinguyinca", "x/" + REPO):
        r = raw(repo=bad)
        assert match_deploy(r, CFG) is None and derive(r, CFG) == derive(r), bad


def test_a_hint_that_disagrees_or_adds_a_host_is_no_match() -> None:
    for hint in ({"repo": "other/repo"}, {"repo": REPO + "x"}, {"host": "prod-box"}, {"host": "hive"}):
        r = raw(hint=hint)
        assert match_deploy(r, CFG) is None and derive(r, CFG).deploy is None, hint


def test_unknown_project_or_config_is_no_match() -> None:
    assert match_deploy(raw(repo="acme/unlisted"), CFG) is None
    assert match_deploy(raw(), {"projects": {}}) is None
    assert match_deploy(raw(), {}) is None
    assert match_deploy(raw(), None) is None
    assert match_deploy(raw(), {"projects": "x"}) is None
    assert match_deploy(raw(), {"projects": {"p": {"repo": REPO, "scopes": 5}}}) is None
    assert derive(raw(), None) == derive(raw()) and derive(raw()).deploy is None


@pytest.mark.parametrize("tool", ["git", "gh", "ssh", "cluster", "slurm", "email", "unknown-tool", ""])
def test_only_shell_like_tools_match(tool: str) -> None:
    assert match_deploy(raw(tool=tool), CFG) is None


def test_non_shell_input_shapes_are_no_match() -> None:
    assert match_deploy(raw(None), CFG) is None
    assert match_deploy(raw(command=None, operation="x"), CFG) is None


def test_existing_classification_of_the_deploy_script_is_untouched_without_config() -> None:
    d = derive(raw())
    assert d.key.verb is Verb.UNKNOWN and d.owner_decides is True and d.deploy is None
    assert check_vetoes(d) is None
