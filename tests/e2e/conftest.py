"""Fixtures for the T16 acceptance suite (marker `e2e`). See README.md."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.e2e import harness


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: T16 end-to-end acceptance (real server processes on loopback)")


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/e2e" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def acceptance():
    return harness.load_acceptance()


@pytest.fixture(scope="session")
def pi_repo(acceptance):
    p = harness.resolve(acceptance, "pi_repo")
    if not (p / "package.json").exists():
        pytest.fail(f"pi_repo {p} is not a Pi checkout (acceptance.yaml)")
    return p


@pytest.fixture(scope="session")
def autospec_bin(acceptance):
    p = harness.resolve(acceptance, "autospec_bin")
    if not p.exists():
        pytest.fail(f"autospec binary missing at {p}: build it with `cargo build -j 4 -p autospec-cli` in the AutoSpec checkout")
    return p


@pytest.fixture
def planes(tmp_path, acceptance):
    """Factory for real control-plane processes; every one is stopped at teardown."""
    made = []

    def make(name="plane", **kw) -> harness.Plane:
        p = harness.Plane(tmp_path / name, name=name, request_timeout=float(acceptance["request_timeout_seconds"]),
                          startup_timeout=float(acceptance["startup_timeout_seconds"]), **kw)
        made.append(p)
        return p.start()

    yield make
    for p in made:
        p.stop()
