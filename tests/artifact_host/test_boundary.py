"""Boundary tests: the artifact host is a standalone package with disjoint role route tables."""

from __future__ import annotations

import ast
from pathlib import Path

from herdr_artifact_host.cli import main
from herdr_artifact_host.roles import Role, routes_for

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "herdr_artifact_host"


def _imported_modules(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def test_no_herdr_engineering_import() -> None:
    sources = sorted(PACKAGE_DIR.rglob("*.py"))
    assert sources, "package has no python sources"
    offenders = [
        f"{path.relative_to(PACKAGE_DIR)}: {name}"
        for path in sources
        for name in _imported_modules(ast.parse(path.read_text(), filename=str(path)))
        if name == "herdr_engineering" or name.startswith("herdr_engineering.")
    ]
    assert offenders == []


def test_internal_routes_absent_from_public_role() -> None:
    public = routes_for(Role.PUBLIC)
    internal = routes_for(Role.INTERNAL)
    assert isinstance(public, frozenset) and isinstance(internal, frozenset)
    assert public.isdisjoint(internal)


def test_cli_rejects_unknown_subcommand() -> None:
    assert main(["bogus"]) == 2


def test_cli_exposes_all_subcommands() -> None:
    for sub in ("public", "internal", "migrate", "import"):
        assert main([sub, "--help"]) == 0
