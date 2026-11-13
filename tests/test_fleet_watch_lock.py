"""Step 5: `fleet.lock.yaml` semantics (spec section 7): `lock.pin_repos`, `herdr-eng fleet lock verify`.

No mocks: real bare git repos play GitHub (the git base is a directory), every command runs as a real subprocess in a
throwaway HOME, and the lock is a real file. Nothing here can touch ~/.config/herdr-fleet or ~/.local/state.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from herdr_engineering.control_plane.fleet_rollout import REASONS, validate_fleet_rollout
from herdr_engineering.fleet_watch import lock, state
from tests.test_fleet_watch import Forge, git
from tests.test_fleet_watch_run import BOOT, HE, PI, fleet  # noqa: F401  (fixture)

AUTO = "berlinguyinca/autospec"
LOCK_TEXT = """# fleet lock - hand written
tools:
  node: "22.1.0"   # keep

repos:
  # the converged heads
  autospec: {auto}   # stray
  pi-engineering: {pi}
other:
  keep: me
"""


@pytest.fixture
def world(tmp_path):
    forge = Forge(tmp_path / "forge")
    for repo in (AUTO, PI, HE):
        forge.create(repo)
    directory = tmp_path / "state"
    path = tmp_path / "fleet.lock.yaml"
    return forge, directory, path


def write_lock(path: Path, forge: Forge) -> str:
    text = LOCK_TEXT.format(auto=forge.head(AUTO), pi=forge.head(PI))
    path.write_text(text)
    return text


def pin(forge, directory, path, updates, **kw):
    return lock.pin_repos(updates, path, directory=directory, base=str(forge.root), **kw)


# --------------------------------------------------------------------------- pin_repos

def test_pin_repos_changes_only_the_repos_values_and_keeps_every_other_byte_and_comment(world):
    forge, directory, path = world
    old = write_lock(path, forge)
    path.chmod(0o640)
    new_auto = forge.commit(AUTO, "two")
    changed = pin(forge, directory, path, {AUTO: new_auto})
    text = path.read_text()
    assert text == old.replace(f"autospec: {forge_old(old, 'autospec')}", f"autospec: {new_auto}")
    assert "# stray" in text and "# the converged heads" in text and "other:\n  keep: me" in text
    assert changed == [{"repo": "autospec", "before": forge_old(old, "autospec"), "after": new_auto}]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert [p.name for p in path.parent.iterdir() if ".tmp" in p.name] == []


def forge_old(text: str, name: str) -> str:
    return re.search(rf"^  {name}: ([0-9a-f]{{40}})", text, re.M).group(1)


def test_pin_repos_keeps_a_backup_of_the_previous_file(world):
    forge, directory, path = world
    old = write_lock(path, forge)
    pin(forge, directory, path, {AUTO: forge.commit(AUTO, "two")})
    (backup,) = [p for p in path.parent.iterdir() if p.name.startswith("fleet.lock.yaml.bak-")]
    assert backup.read_text() == old
    assert re.fullmatch(r"fleet\.lock\.yaml\.bak-\d{8}(-\d{6})?", backup.name)
    pin(forge, directory, path, {AUTO: forge.commit(AUTO, "three")})         # a second write the same day
    backups = sorted(p for p in path.parent.iterdir() if p.name.startswith("fleet.lock.yaml.bak-"))
    assert len(backups) == 2 and old in {b.read_text() for b in backups}


def test_pin_repos_adds_a_missing_repo_key_inside_the_repos_block(world):
    forge, directory, path = world
    old = write_lock(path, forge)
    head = forge.head(HE)
    pin(forge, directory, path, {HE: head})
    text = path.read_text()
    assert f"  herdr-engineering: {head}\nother:" in text
    assert text.startswith(old.split("repos:")[0])


def test_pin_repos_without_a_repos_block_appends_one_and_without_a_file_needs_create(world):
    forge, directory, path = world
    path.write_text("tools:\n  node: 1\n")
    head = forge.head(AUTO)
    pin(forge, directory, path, {AUTO: head})
    assert path.read_text() == f"tools:\n  node: 1\nrepos:\n  autospec: {head}\n"
    missing = path.parent / "absent.yaml"
    with pytest.raises(lock.LockRefused) as exc:
        pin(forge, directory, missing, {AUTO: head})
    assert exc.value.reason == "lock_invalid" and not missing.exists()
    pin(forge, directory, missing, {AUTO: head}, create=True)
    assert missing.read_text() == f"repos:\n  autospec: {head}\n"


def test_an_unpublished_sha_refuses_the_whole_write_and_leaves_the_file_untouched(world):
    forge, directory, path = world
    old = write_lock(path, forge)
    good = forge.commit(PI, "two")
    stray = forge.commit(AUTO, "stray", push=False)                          # exists locally, never pushed
    with pytest.raises(lock.LockRefused) as exc:
        pin(forge, directory, path, {PI: good, AUTO: stray})
    assert exc.value.reason == "not_published" and exc.value.repos == ["autospec"]
    assert path.read_text() == old                                           # the good pin was not applied either
    assert not [p for p in path.parent.iterdir() if "bak" in p.name or "tmp" in p.name]


def test_a_sha_that_a_force_push_removed_from_main_is_refused(world):
    forge, directory, path = world
    old = write_lock(path, forge)
    doomed = forge.commit(AUTO, "doomed")
    pin(forge, directory, path, {AUTO: doomed})                              # fills the cache while it is on main
    work = forge.work[AUTO]
    git(work, "reset", "-q", "--hard", "HEAD~1")
    git(work, "push", "-q", "-f", "origin", "main")
    path.write_text(old)
    with pytest.raises(lock.LockRefused) as exc:
        pin(forge, directory, path, {AUTO: doomed})
    assert exc.value.reason == "not_published"


def test_a_malformed_sha_or_unwatched_repo_is_refused(world):
    forge, directory, path = world
    write_lock(path, forge)
    with pytest.raises(lock.LockRefused) as exc:
        pin(forge, directory, path, {AUTO: "main"})
    assert exc.value.reason == "not_published"
    with pytest.raises(lock.LockRefused) as exc:
        pin(forge, directory, path, {"berlinguyinca/not-watched": forge.head(AUTO)})
    assert exc.value.reason == "lock_invalid"


def test_a_downgrade_needs_allow_downgrade(world):
    forge, directory, path = world
    old_head = forge.head(AUTO)
    newer = forge.commit(AUTO, "two")
    path.write_text(f"repos:\n  autospec: {newer}\n")
    with pytest.raises(lock.LockRefused) as exc:
        pin(forge, directory, path, {AUTO: old_head})
    assert exc.value.reason == "downgrade"
    assert path.read_text() == f"repos:\n  autospec: {newer}\n"
    pin(forge, directory, path, {AUTO: old_head}, allow_downgrade=True)
    assert path.read_text() == f"repos:\n  autospec: {old_head}\n"


def test_pinning_the_value_already_there_is_a_no_op_without_a_backup(world):
    forge, directory, path = world
    old = write_lock(path, forge)
    assert pin(forge, directory, path, {AUTO: forge.head(AUTO)}) == []
    assert path.read_text() == old
    assert not [p for p in path.parent.iterdir() if "bak" in p.name]


def test_a_failed_write_leaves_the_original_and_no_temp_file(world):
    forge, directory, path = world
    old = write_lock(path, forge)
    path.parent.chmod(0o500)
    try:
        with pytest.raises(OSError):
            pin(forge, directory, path, {AUTO: forge.commit(AUTO, "two")})
    finally:
        path.parent.chmod(0o700)
    assert path.read_text() == old
    assert not [p for p in path.parent.iterdir() if "tmp" in p.name]


def test_lock_refusal_reasons_are_closed_enums_the_control_plane_accepts():
    for reason in ("not_published", "downgrade", "lock_invalid"):
        assert reason in REASONS
        validate_fleet_rollout("0123456789abcdef", "lock.refused",
                               {"rollout_id": "0123456789abcdef", "ts": "2026-11-11T00:00:00+00:00", "seq": 1,
                                "targets": [{"repo": "autospec", "sha": "a" * 40}], "reason": reason})


# --------------------------------------------------------------------------- fleet lock verify (CLI)

def verify_cli(forge, directory_home: Path, path: Path, *extra: str):
    env = {**os.environ, "HOME": str(directory_home), "XDG_STATE_HOME": str(directory_home / "state"),
           "HERDR_FLEET_WATCH_GIT_BASE": str(forge.root), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run([sys.executable, "-I", "-c", BOOT, "fleet", "lock", "verify", "--lock", str(path), *extra],
                          capture_output=True, text=True, env=env, timeout=120)


def test_lock_verify_passes_for_published_pins_and_ignores_unwatched_keys(world, tmp_path):
    forge, _, path = world
    write_lock(path, forge)
    out = verify_cli(forge, tmp_path / "home", path, "--json")
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    assert {p["repo"]: p["status"] for p in report["pins"]} == {"autospec": "ok", "pi-engineering": "ok"}
    assert report["ok"] is True


def test_lock_verify_names_the_unpublished_pin_and_exits_non_zero(world, tmp_path):
    forge, _, path = world
    stray = forge.commit(AUTO, "stray", push=False)           # the 6b7b8eb0 hazard: a pin to an unpushed commit
    path.write_text(f"repos:\n  autospec: {stray}\n  pi-engineering: {forge.head(PI)}\n")
    out = verify_cli(forge, tmp_path / "home", path, "--json")
    assert out.returncode == 1
    report = json.loads(out.stdout)
    by = {p["repo"]: p for p in report["pins"]}
    assert by["autospec"] == {"repo": "autospec", "sha": stray, "status": "unknown_commit"}
    assert by["pi-engineering"]["status"] == "ok" and report["ok"] is False
    text = verify_cli(forge, tmp_path / "home", path)
    assert text.returncode == 1 and "autospec" in text.stdout and "unknown_commit" in text.stdout
    assert stray[:12] in text.stdout


def test_lock_verify_reports_a_pin_that_left_origin_main_and_a_malformed_pin(world, tmp_path):
    forge, _, path = world
    doomed = forge.commit(AUTO, "doomed")
    path.write_text(f"repos:\n  autospec: {doomed}\n")
    assert verify_cli(forge, tmp_path / "home", path).returncode == 0         # published now
    work = forge.work[AUTO]
    git(work, "reset", "-q", "--hard", "HEAD~1")
    git(work, "push", "-q", "-f", "origin", "main")
    out = verify_cli(forge, tmp_path / "home", path, "--json")
    assert out.returncode == 1 and json.loads(out.stdout)["pins"][0]["status"] == "not_on_origin_main"
    path.write_text("repos:\n  autospec: latest\n")
    bad = verify_cli(forge, tmp_path / "home", path, "--json")
    assert bad.returncode == 1 and json.loads(bad.stdout)["pins"][0]["status"] == "malformed_sha"


def test_lock_verify_reports_a_pin_that_differs_from_the_converged_state(world, tmp_path):
    from herdr_engineering.fleet_watch import state
    forge, _, path = world
    home = tmp_path / "home"
    older = forge.head(AUTO)
    newer = forge.commit(AUTO, "two")
    path.write_text(f"repos:\n  autospec: {older}\n")
    saved = state.empty_state()
    saved["converged"][AUTO] = newer
    state.save(saved, home / "state/herdr-engineering/fleet-watch")
    out = verify_cli(forge, home, path, "--json")
    assert out.returncode == 1 and json.loads(out.stdout)["pins"][0]["status"] == "converged_mismatch"


def test_lock_verify_on_a_missing_or_unreadable_lock_is_an_error_not_a_pass(world, tmp_path):
    forge, _, path = world
    out = verify_cli(forge, tmp_path / "home", tmp_path / "nope.yaml")
    assert out.returncode == 2 and "lock" in out.stderr
    path.write_text("repos: [unclosed\n")
    assert verify_cli(forge, tmp_path / "home", path).returncode == 2


def test_lock_verify_output_is_closed_vocabulary(world, tmp_path):
    forge, _, path = world
    path.write_text(f"repos:\n  autospec: {forge.head(AUTO)}\n  ai-sync: {'a' * 40}\n  weird name: x\n")
    out = verify_cli(forge, tmp_path / "home", path, "--json")
    for pin_row in json.loads(out.stdout)["pins"]:
        assert set(pin_row) == {"repo", "sha", "status"}
        assert pin_row["status"] in lock.PIN_STATUSES and re.fullmatch(r"[a-z-]{1,40}", pin_row["repo"])
    assert "weird" not in out.stdout


# --------------------------------------------------------------------------- the runner moves the lock

def lock_events(fleet):
    return [e for e in fleet.queued() if e["event_type"].startswith("lock.")]


def test_the_lock_moves_only_after_a_fully_succeeded_rollout(fleet):
    base_sha = git(fleet.forge.work[HE], "rev-parse", "HEAD~1")
    fleet.lock.write_text(f"tools:\n  x: 1\nrepos:\n  herdr-engineering: {base_sha}\n")
    (fleet.checkout("bender") / "dirty").write_text("x")
    halted = fleet.cli()
    assert halted.returncode == 1
    assert base_sha in fleet.lock.read_text() and lock_events(fleet) == []        # a halt leaves the lock alone
    (fleet.checkout("bender") / "dirty").unlink()
    done = fleet.cli("--resume")
    head = fleet.forge.head(HE)
    assert done.returncode == 0, done.stderr
    assert fleet.lock.read_text() == f"tools:\n  x: 1\nrepos:\n  herdr-engineering: {head}\n"
    assert fleet.types()[-2:] == ["rollout.succeeded", "lock.pinned"]
    pinned = lock_events(fleet)[0]["payload"]
    assert pinned["targets"] == [{"repo": "herdr-engineering", "sha": head, "before": base_sha}]
    assert fleet.saved()["rollouts"][pinned["rollout_id"]]["lock"] == "pinned"


def test_a_refused_pin_is_recorded_but_does_not_undo_the_converged_rollout(fleet):
    head = fleet.forge.head(HE)
    ahead = fleet.forge.commit(HE, "three")                                   # the lock is ahead of what main will be
    fleet.lock.write_text(f"repos:\n  herdr-engineering: {ahead}\n")
    assert subprocess.run([sys.executable, "-I", "-c", BOOT, "fleet", "lock", "verify", "--lock", str(fleet.lock)],
                          env=fleet.env(), capture_output=True).returncode == 0   # caches `ahead` while published
    work = fleet.forge.work[HE]
    git(work, "reset", "-q", "--hard", "HEAD~1")
    git(work, "push", "-q", "-f", "origin", "main")
    assert fleet.forge.head(HE) == head
    out = fleet.cli()
    assert out.returncode == 0, out.stderr
    assert fleet.lock.read_text() == f"repos:\n  herdr-engineering: {ahead}\n"
    refused = lock_events(fleet)
    assert [e["event_type"] for e in refused] == ["lock.refused"] and refused[0]["payload"]["reason"] == "downgrade"
    saved = fleet.saved()
    assert saved["converged"] == {HE: head} and saved["rollouts"][refused[0]["entity_id"]]["lock"] == "refused"


def test_a_crash_between_succeeded_and_the_pin_is_repaired_by_the_next_run(fleet):
    base_sha = git(fleet.forge.work[HE], "rev-parse", "HEAD~1")
    fleet.lock.write_text(f"repos:\n  herdr-engineering: {base_sha}\n")
    assert fleet.cli().returncode == 0
    head = fleet.forge.head(HE)
    saved = fleet.saved()
    (rid,) = saved["rollouts"]
    saved["rollouts"][rid]["lock"] = "pending"                              # the crash lost the pin outcome
    fleet.lock.write_text(f"repos:\n  herdr-engineering: {base_sha}\n")
    from herdr_engineering.fleet_watch import state
    state.save(saved, fleet.state_dir)
    again = fleet.cli()
    assert again.returncode == 0, again.stderr
    assert f"herdr-engineering: {head}" in fleet.lock.read_text()
    assert fleet.saved()["rollouts"][rid]["lock"] == "pinned"


def test_fake_secret_shapes_never_reach_lock_events(fleet):
    # the lock path accepts only 40-hex SHAs for known repos; anything else is refused before an event exists
    key = "sk-" + os.urandom(16).hex()
    with pytest.raises(lock.LockRefused):
        lock.pin_repos({HE: key}, fleet.lock, directory=fleet.state_dir, base=str(fleet.forge.root), create=True)
    assert key not in json.dumps(fleet.queued())



# --------------------------------------------------------------------------- review fixes

def test_pin_repos_refuses_when_the_lock_changed_after_it_was_read(world):
    forge, directory, path = world
    old = write_lock(path, forge)
    path.write_text(old + "# edited by the operator meanwhile\n")
    with pytest.raises(lock.LockRefused) as caught:
        lock._write_atomic(path, old.replace("a", "a"), 0o600, old.encode(), datetime.now(UTC))
    assert caught.value.reason == "lock_invalid"
    assert path.read_text().endswith("meanwhile\n")
    assert not [p for p in path.parent.iterdir() if "tmp" in p.name or "bak" in p.name]


def test_a_symlinked_lock_stays_a_symlink_and_its_target_is_updated(world, tmp_path):
    forge, directory, path = world
    real = tmp_path / "real" / "fleet.lock.yaml"
    real.parent.mkdir()
    write_lock(real, forge)
    path.symlink_to(real)
    new = forge.commit(AUTO, "two")
    pin(forge, directory, path, {AUTO: new})
    assert path.is_symlink() and new in real.read_text()
    assert [p.name for p in real.parent.iterdir() if "bak" in p.name]


def test_lock_verify_while_the_watcher_holds_its_lock_reports_busy_not_a_fetch_failure(world, tmp_path):
    forge, _, path = world
    write_lock(path, forge)
    home = tmp_path / "home"
    held = home / "state" / "herdr-engineering" / "fleet-watch"
    with state.single_instance(held):
        out = verify_cli(forge, home, path, "--json")
    assert out.returncode == 3 and json.loads(out.stdout) == {"ok": False, "status": "busy"}
    assert "fetch_failed" not in out.stdout


def test_a_rollout_without_a_lock_file_emits_lock_refused_lock_invalid_but_never_creates_it(fleet):
    assert fleet.cli().returncode == 0
    assert not fleet.lock.exists()
    events = lock_events(fleet)
    assert [e["event_type"] for e in events] == ["lock.refused"] and events[0]["payload"]["reason"] == "lock_invalid"
