"""Boundary tests: the artifact host is a standalone package with disjoint role route tables."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import MappingProxyType

import herdr_artifact_host
from herdr_artifact_host import roles
from herdr_artifact_host.cli import main
from herdr_artifact_host.roles import Role, routes_for

REPO = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO / "herdr_artifact_host"
FORBIDDEN = "herdr_engineering"


def violations_in_source(source: str, filename: str = "<src>") -> list[str]:
    """Every way a module could reach the control-plane package, statically."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if a.name.split(".")[0] == FORBIDDEN]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.split(".")[0] == FORBIDDEN:
                found.append(f"from {node.module}")
            elif node.level >= 2:
                found.append(f"relative import climbing out (level={node.level})")
            elif node.level >= 1 and node.module is None and node.names:
                found.append("bare relative 'from . import' ")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if FORBIDDEN in node.value:
                found.append(f"string constant mentioning {FORBIDDEN}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "__import__":
                found.append("__import__ call")
            elif isinstance(fn, ast.Attribute) and fn.attr == "import_module":
                found.append("import_module call")
            elif isinstance(fn, ast.Name) and fn.id == "import_module":
                found.append("import_module call")
    return found


def violations_in_package(package_dir: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        out += [
            f"{path.relative_to(package_dir)}: {v}"
            for v in violations_in_source(path.read_text(), str(path))
        ]
    return out


def test_no_herdr_engineering_import() -> None:
    assert list(PACKAGE_DIR.rglob("*.py")), "package has no python sources"
    assert violations_in_package(PACKAGE_DIR) == []


def test_checker_catches_each_bad_pattern(tmp_path: Path) -> None:
    bad = {
        "plain_import": "import herdr_engineering.cli\n",
        "from_import": "from herdr_engineering import cli\n",
        "string_constant": "NAME = 'herdr_engineering.cli'\n",
        "climbing_relative": "from .. import something\n",
        "climbing_relative_module": "from ..pkg import something\n",
        "bare_relative": "from . import sibling\n",
        "dunder_import": "m = __import__('x')\n",
        "import_module_attr": "import importlib\nm = importlib.import_module('x')\n",
        "import_module_name": "from importlib import import_module\nm = import_module('x')\n",
    }
    pkg = tmp_path / "herdr_artifact_host"
    pkg.mkdir()
    for name, src in bad.items():
        (pkg / f"{name}.py").write_text(src)
        assert violations_in_source(src), f"checker missed {name}"
    reported = violations_in_package(pkg)
    for name in bad:
        assert any(r.startswith(f"{name}.py:") for r in reported), name


def test_checker_accepts_clean_source() -> None:
    assert violations_in_source("import json\nfrom .roles import Role\n") == []


def _run_isolated(paths: list[str]) -> subprocess.CompletedProcess[str]:
    """Import the host in a ``-I`` interpreter (REPO and bad copies injected via sys.path)."""
    code = (
        f"import sys; sys.path[:0] = {paths!r}; "
        "import herdr_artifact_host.cli, herdr_artifact_host.roles; "
        "assert not any(m.split('.')[0]=='herdr_engineering' for m in sys.modules)"
    )
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)


def test_runtime_import_does_not_load_control_plane() -> None:
    result = _run_isolated([str(REPO)])
    assert result.returncode == 0, result.stderr


def test_runtime_check_fails_on_bad_copy(tmp_path: Path) -> None:
    pkg = tmp_path / "bad_host" / "herdr_artifact_host"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "roles.py").write_text("")
    (pkg / "cli.py").write_text("import sys\nsys.modules.setdefault('herdr_engineering', sys)\n")
    result = _run_isolated([str(tmp_path / "bad_host"), str(REPO)])
    assert result.returncode != 0


def test_internal_routes_absent_from_public_role() -> None:
    public = routes_for(Role.PUBLIC)
    internal = routes_for(Role.INTERNAL)
    assert isinstance(public, frozenset) and isinstance(internal, frozenset)
    assert public.isdisjoint(internal)


def test_route_table_is_immutable() -> None:
    assert isinstance(roles._ROUTES, MappingProxyType)
    assert all(isinstance(v, frozenset) for v in roles._ROUTES.values())


def test_cli_rejects_unknown_subcommand() -> None:
    assert main(["bogus"]) == 2


def test_cli_exposes_all_subcommands() -> None:
    for sub in ("public", "internal", "migrate", "import"):
        assert main([sub, "--help"]) == 0


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def _dep_names(deps: list[str]) -> set[str]:
    return {re.split(r"[\s\[<>=!~;]", d, maxsplit=1)[0].lower() for d in deps}


def test_host_manifest_matches_package() -> None:
    host = _toml(REPO / "deploy" / "artifact-host" / "pyproject.toml")
    root = _toml(REPO / "pyproject.toml")
    assert host["project"]["version"] == herdr_artifact_host.__version__
    assert host["project"]["scripts"] == {"herdr-artifact-host": "herdr_artifact_host.cli:main"}
    assert host["project"]["requires-python"] == root["project"]["requires-python"]
    assert host["tool"]["setuptools"]["package-data"] == {"herdr_artifact_host": ["share_static/*", "migrations/*.sql"]}
    assert host["tool"]["setuptools"]["packages"]["find"]["include"] == ["herdr_artifact_host*"]
    root_deps = set(root["project"]["dependencies"])
    for extra in root["project"]["optional-dependencies"].values():
        root_deps |= set(extra)
    assert _dep_names(host["project"]["dependencies"]) <= _dep_names(sorted(root_deps))
    assert "herdr_engineering" not in _dep_names(host["project"]["dependencies"])


def test_control_plane_wheel_excludes_host() -> None:
    root = _toml(REPO / "pyproject.toml")
    find = root["tool"]["setuptools"]["packages"]["find"]
    assert find["include"] == ["herdr_engineering*"]
    assert "herdr-artifact-host" not in root["project"]["scripts"]
    assert "herdr_artifact_host" not in root["tool"]["setuptools"]["package-data"]


# Tests that deliberately name the control plane: this file (it defines the rule) and the token cross-check, which
# compares the host's generated tokens.css with the control plane's file by path. Delete the second with the originals.
TEST_EXCEPTIONS = {"test_boundary.py", "test_share_tokens.py"}


def test_host_tests_do_not_reference_the_control_plane():
    tests_dir = Path(__file__).parent
    offenders = [
        p.name
        for p in sorted(tests_dir.glob("*.py"))
        if p.name not in TEST_EXCEPTIONS and FORBIDDEN in p.read_text()
    ]
    assert offenders == []
