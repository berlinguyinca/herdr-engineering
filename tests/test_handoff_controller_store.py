"""One handoff store for every repo, and the one-off import of the old per-repo stores."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from herdr_engineering.handoff_controller.handoff_cli import HandoffCli
from herdr_engineering.handoff_controller.store import import_legacy_args, shared_store_root
from tests.test_handoff_controller_briefing import _cli_prefix, create_record

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def handoff() -> HandoffCli:
    prefix, why = _cli_prefix()
    if prefix is None:
        pytest.skip(why)
    return HandoffCli(prefix)


def _importer():
    spec = importlib.util.spec_from_file_location("handoff_import_legacy", ROOT / "scripts" / "handoff_import_legacy.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_store_root_follows_the_documented_rule(tmp_path: Path) -> None:
    home = {"HOME": str(tmp_path)}
    assert shared_store_root(home) == tmp_path / ".local" / "share" / "herdr-handoff" / "store"
    assert shared_store_root({**home, "HERDR_HANDOFF_STORE": "/abs/store"}) == Path("/abs/store")
    assert shared_store_root({**home, "HERDR_HANDOFF_STORE": "~/s"}) == tmp_path / "s"
    assert shared_store_root({**home, "HERDR_HANDOFF_STORE": "~"}) == tmp_path
    assert shared_store_root({**home, "HERDR_HANDOFF_STORE": "~other/s"}) == Path("~other/s")
    assert shared_store_root({**home, "HERDR_HANDOFF_STORE": ""}) == tmp_path / ".local" / "share" / "herdr-handoff" / "store"


def test_import_legacy_args_name_the_shared_root_and_the_repos_old_store() -> None:
    assert import_legacy_args(Path("/s"), Path("/r")) == ["--root", "/s", "store", "import-legacy", "/r/.herdr/handoffs"]


def test_records_from_two_repos_land_in_one_store(tmp_path: Path, handoff: HandoffCli) -> None:
    store = shared_store_root({"HOME": str(tmp_path)})
    ids = []
    for name in ("one", "two"):
        repo = tmp_path / name
        repo.mkdir()
        ids.append(create_record(handoff, store, f"p-{name}", goal=f"goal {name}"))  # the controller passes this root for any repo
    listed = handoff.run(["--root", str(store), "list"], timeout_s=60)
    assert listed.returncode == 0, listed.stderr
    found = {r["handoff_id"] for r in json.loads(listed.stdout)}
    assert set(ids) <= found
    assert not (tmp_path / "one" / ".herdr").exists() and not (tmp_path / "two" / ".herdr").exists()


def test_legacy_records_imported_once(tmp_path: Path, handoff: HandoffCli) -> None:
    repo = tmp_path / "repo"
    old = repo / ".herdr" / "handoffs"
    old_id = create_record(handoff, old, "p-old", goal="an old record")
    store = tmp_path / "shared"
    prefix, _ = _cli_prefix()
    assert prefix is not None
    script = _importer()
    env = {"HOME": str(tmp_path), "HERDR_HANDOFF_STORE": str(store)}
    assert script.main([str(repo)], env, tuple(prefix)) == 0
    assert (store / "outbox" / f"{old_id}.json").is_file()
    assert (old / "outbox" / f"{old_id}.json").is_file()  # the old store is never touched
    before = (store / "outbox" / f"{old_id}.json").read_text()
    assert script.main([str(repo)], env, tuple(prefix)) == 0  # again: nothing new, nothing changed
    assert sorted(p.name for p in (store / "outbox").glob("*.json")) == [f"{old_id}.json"]
    assert (store / "outbox" / f"{old_id}.json").read_text() == before


def test_the_import_script_reports_a_failure(tmp_path: Path) -> None:
    script = _importer()
    assert script.main([str(tmp_path)], {"HOME": str(tmp_path)}, (sys.executable, "-c", "import sys; sys.exit(3)")) == 1
