"""``personal_assistant.deploy`` config normalization: strict, fail closed, ``mode: auto`` is only a candidate."""
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from herdr_engineering.control_plane.assistant import assistant_config

REQUIRE_ALL = ["on_main", "ci_green", "descends_from_deployed"]
GOOD = {"projects": {"herdr-engineering": {
    "repo": "berlinguyinca/herdr-engineering",
    "scopes": {"control-plane": {"mode": "ask", "command": "deploy/control-plane/deploy.sh",
                                 "require": list(REQUIRE_ALL),
                                 "health_url": "http://127.0.0.1:8080/api/v1/health"}}}}}


def _deploy(deploy: Any = None) -> dict[str, Any]:
    pa: dict[str, Any] = {} if deploy is None else {"deploy": deploy}
    return assistant_config({"personal_assistant": pa})["deploy"]


def _with(**scope: Any) -> dict[str, Any]:
    cfg = deepcopy(GOOD)
    cfg["projects"]["herdr-engineering"]["scopes"]["control-plane"].update(scope)
    return cfg


def test_defaults_are_empty_and_wired_into_assistant_config() -> None:
    assert _deploy() == {"projects": {}}
    assert assistant_config({})["deploy"] == {"projects": {}}


def test_a_good_block_normalizes() -> None:
    assert _deploy(GOOD) == GOOD


def test_mode_defaults_to_ask_and_require_to_all_three() -> None:
    cfg = {"projects": {"p": {"repo": "o/r", "scopes": {"s": {"command": "deploy.sh"}}}}}
    scope = _deploy(cfg)["projects"]["p"]["scopes"]["s"]
    assert scope == {"mode": "ask", "command": "deploy.sh", "require": REQUIRE_ALL}   # default list: no health_url needed


def test_require_subset_is_kept_in_canonical_order_and_deduplicated_input_is_refused() -> None:
    scope = _deploy(_with(require=["ci_green", "on_main"]))["projects"]["herdr-engineering"]["scopes"]["control-plane"]
    assert scope["require"] == ["on_main", "ci_green"]
    with pytest.raises(ValueError, match="duplicate"):
        _deploy(_with(require=["on_main", "on_main"]))


def test_mode_auto_is_accepted_as_a_candidate() -> None:
    scope = _deploy(_with(mode="auto"))["projects"]["herdr-engineering"]["scopes"]["control-plane"]
    assert scope["mode"] == "auto"


@pytest.mark.parametrize("bad", ["", "onwards", "AUTO", "Ask", True, 1, None, ["auto"]])
def test_bad_mode(bad: Any) -> None:
    with pytest.raises(ValueError, match="mode"):
        _deploy(_with(mode=bad))


@pytest.mark.parametrize("bad", [[], ["deploy"], ["on_main", "nope"], "on_main", [1], [None], ["ON_MAIN"], None])
def test_bad_require(bad: Any) -> None:
    with pytest.raises(ValueError, match="require"):
        _deploy(_with(require=bad))


@pytest.mark.parametrize("bad", [
    "", "   ", "/abs/deploy.sh", "../deploy.sh", "a/../deploy.sh", "./deploy.sh", "a/./b.sh", "a//b.sh", "a/",
    "deploy sh", "deploy.sh ", " deploy.sh", "deploy.sh\n", "a\tb", "deploy.sh;ls", "a&&b", "a|b", "$(x)", "`x`",
    "a>b", "a<b", "a*b", "a?b", "a~b", "a'b", 'a"b', "a\\b", "a#b", "a!b", "a(b", "-deploy.sh", "x" * 300, None, 5,
    ["deploy.sh"], "~/deploy.sh", "a{b}", "a=b", "a,b", "a:b", "é.sh",
])
def test_bad_command(bad: Any) -> None:
    with pytest.raises(ValueError, match="command"):
        _deploy(_with(command=bad))


def test_missing_command_is_refused() -> None:
    cfg = _with()
    del cfg["projects"]["herdr-engineering"]["scopes"]["control-plane"]["command"]
    with pytest.raises(ValueError, match="command"):
        _deploy(cfg)


@pytest.mark.parametrize("bad", ["", "repo", "a/b/c", "/r", "o/", "o/ r", "o/r\n", "o/..", "../r", "o/r;x", 5, None,
                                 "o/r.git/", "o//r"])
def test_bad_repo(bad: Any) -> None:
    cfg = deepcopy(GOOD)
    cfg["projects"]["herdr-engineering"]["repo"] = bad
    with pytest.raises(ValueError, match="repo"):
        _deploy(cfg)


def test_missing_repo_and_scopes_are_refused() -> None:
    with pytest.raises(ValueError, match="repo"):
        _deploy({"projects": {"p": {"scopes": {"s": {"command": "d.sh"}}}}})
    with pytest.raises(ValueError, match="scopes"):
        _deploy({"projects": {"p": {"repo": "o/r"}}})
    with pytest.raises(ValueError, match="scopes"):
        _deploy({"projects": {"p": {"repo": "o/r", "scopes": {}}}})


@pytest.mark.parametrize("bad", ["", "-a", "_a", "A", "a b", "a/b", "a.b", "a" * 65, "é", "a\n", 5, True])
def test_bad_project_and_scope_ids(bad: Any) -> None:
    with pytest.raises(ValueError, match="project id"):
        _deploy({"projects": {bad: GOOD["projects"]["herdr-engineering"]}})
    scope = GOOD["projects"]["herdr-engineering"]["scopes"]["control-plane"]
    with pytest.raises(ValueError, match="scope id"):
        _deploy({"projects": {"p": {"repo": "o/r", "scopes": {bad: scope}}}})


def test_good_ids_include_digits_dash_underscore_and_the_length_limit() -> None:
    scope = {"command": "d.sh"}
    pid = "a" + "b" * 63
    assert pid in _deploy({"projects": {pid: {"repo": "o/r", "scopes": {"0_a-b": scope}}}})["projects"]


@pytest.mark.parametrize("where", ["top", "project", "scope"])
def test_unknown_keys_are_refused(where: str) -> None:
    cfg = deepcopy(GOOD)
    target = {"top": cfg, "project": cfg["projects"]["herdr-engineering"],
              "scope": cfg["projects"]["herdr-engineering"]["scopes"]["control-plane"]}[where]
    target["surprise"] = 1
    with pytest.raises(ValueError, match="unknown keys"):
        _deploy(cfg)


@pytest.mark.parametrize("name", ["token", "secret", "password", "api_key", "key", "bot_token", "webhook_url"])
def test_inline_secrets_are_refused_with_a_clear_message(name: str) -> None:
    cfg = deepcopy(GOOD)
    cfg["projects"]["herdr-engineering"]["scopes"]["control-plane"][name] = "hunter2"
    with pytest.raises(ValueError, match="secrets") as exc:
        _deploy(cfg)
    assert "hunter2" not in str(exc.value)


def test_duplicate_repo_and_command_across_scopes_is_refused() -> None:
    cfg = deepcopy(GOOD)
    cfg["projects"]["herdr-engineering"]["scopes"]["again"] = {"command": "deploy/control-plane/deploy.sh"}
    with pytest.raises(ValueError, match="duplicate"):
        _deploy(cfg)


def test_duplicate_repo_and_command_across_projects_is_refused_even_with_different_case() -> None:
    cfg = deepcopy(GOOD)
    cfg["projects"]["other"] = {"repo": "BerlinGuyInca/Herdr-Engineering",
                                "scopes": {"x": {"command": "deploy/control-plane/deploy.sh"}}}
    with pytest.raises(ValueError, match="duplicate"):
        _deploy(cfg)


def test_same_command_in_different_repos_is_fine() -> None:
    cfg = deepcopy(GOOD)
    cfg["projects"]["other"] = {"repo": "acme/other", "scopes": {"x": {"command": "deploy/control-plane/deploy.sh"}}}
    assert len(_deploy(cfg)["projects"]) == 2


@pytest.mark.parametrize("bad", ["x", [], 5, True])
def test_shape_errors(bad: Any) -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        _deploy(bad)
    with pytest.raises(ValueError, match="projects"):
        _deploy({"projects": bad})
    with pytest.raises(ValueError, match="projects"):
        _deploy({"projects": None})


def test_scope_must_be_a_mapping() -> None:
    with pytest.raises(ValueError, match="mapping"):
        _deploy({"projects": {"p": {"repo": "o/r", "scopes": {"s": "deploy.sh"}}}})


def test_example_yaml_deploy_block_parses_and_validates() -> None:
    text = (Path(__file__).resolve().parents[2] / "deploy/control-plane/fleet-config.example.yaml").read_text()
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.fullmatch(r"\s*#   deploy:", ln))
    block: list[str] = []
    for ln in lines[start:]:
        m = re.fullmatch(r"      #(.*)", ln)
        if m is None or (block and re.match(r"   \S", m.group(1))):
            break
        block.append(m.group(1)[3:] if m.group(1).startswith("   ") else m.group(1))
    parsed = yaml.safe_load("\n".join(block))["deploy"]
    cfg = _deploy(parsed)
    (project,) = cfg["projects"].values()
    (scope,) = project["scopes"].values()
    assert scope["mode"] == "ask" and scope["require"] == REQUIRE_ALL      # shipped example never auto-deploys
