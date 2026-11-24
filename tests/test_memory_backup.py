"""deploy/control-plane/memory-backup.sh against a stub `docker` (pg_dump/pg_restore/psql are scripted); the user units
must follow the repo's unit conventions. Nothing real is contacted. `--target control-plane` covers the main
control-plane database and its object store with the same script."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CP = ROOT / "deploy" / "control-plane"
SCRIPT = CP / "memory-backup.sh"
GNU = "/opt/homebrew/opt/coreutils/libexec/gnubin"      # GNU stat/date/head on a dev Mac; the script targets Linux
pytestmark = pytest.mark.skipif(os.uname().sysname != "Linux" and not os.path.isdir(GNU),
                                reason="needs GNU coreutils (stat -c, head -n -N, date +%N)")


@pytest.fixture
def b(tmp_path):
    bindir, home = tmp_path / "bin", tmp_path / "home"
    bindir.mkdir()
    home.mkdir()
    log = tmp_path / "log"
    log.write_text("")
    docker = bindir / "docker"
    docker.write_text(f'''#!/bin/bash
echo "docker $*" >> {log}
case "$*" in
  *pg_dump*) [ -z "${{FAIL_DUMP:-}}" ] || exit 1; [ -z "${{EMPTY_DUMP:-}}" ] && printf 'PGDMP-synthetic' || true ;;
  *"pg_restore --list"*) cat >/dev/null; [ -z "${{FAIL_LIST:-}}" ] || exit 1 ;;
  *"pg_restore -U"*) cat >/dev/null; [ -z "${{FAIL_RESTORE:-}}" ] || exit 1 ;;
  *"select count"*) echo "${{TABLES:-7}}" ;;
  *"printenv POSTGRES_USER"*) echo "${{CP_USER-cpadmin}}" ;;
  *"cp herdr-cp-rustfs:/data -"*) [ -z "${{FAIL_OBJECTS:-}}" ] || exit 1; tar -cf - -C "$OBJ_SRC" . ;;
esac
exit 0
''')
    docker.chmod(0o755)
    (bindir / "systemctl").write_text(f'#!/bin/bash\necho "systemctl $*" >> {log}\n')
    (bindir / "systemctl").chmod(0o755)
    objects = tmp_path / "objects"
    (objects / "data" / "bucket").mkdir(parents=True)
    (objects / "data" / "bucket" / "payload").write_text("artifact bytes")
    env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{GNU}:{os.environ['PATH']}", OBJ_SRC=str(objects))
    dest = tmp_path / "backups"

    class B:
        pass
    o = B()
    o.dest, o.log, o.home, o.env = dest, log, home, env

    def run(*args, **extra):
        return subprocess.run(["bash", str(SCRIPT), *args], env=dict(env, **extra), capture_output=True, text=True,
                              timeout=60)
    o.run = run
    return o


def test_backup_writes_an_owner_only_verified_dump_and_reports_its_checksum(b):
    r = b.run("backup", "--dest", str(b.dest))
    assert r.returncode == 0, r.stderr
    info = json.loads(r.stdout)
    f = Path(info["path"])
    assert f.name.startswith("memory-") and f.suffix == ".dump" and f.read_bytes() == b"PGDMP-synthetic"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(b.dest.stat().st_mode) == 0o700
    assert info["format_verified"] is True and len(info["sha256"]) == 64
    assert list(b.dest.glob(".partial-*")) == []
    log = b.log.read_text()
    assert "exec herdr-hindsight-db pg_dump -U hindsight -d hindsight --format=custom --no-password" in log
    assert "pg_restore --list" in log                      # the archive is verified before it is published


@pytest.mark.parametrize("env", [{"FAIL_DUMP": "1"}, {"FAIL_LIST": "1"}, {"EMPTY_DUMP": "1"}])
def test_a_failed_or_unverifiable_backup_leaves_nothing_behind(b, env):
    r = b.run("backup", "--dest", str(b.dest), **env)
    assert r.returncode == 1 and "error" in json.loads(r.stdout)
    assert list(b.dest.iterdir()) == []


def test_backup_refuses_a_symlinked_destination(b, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    r = b.run("backup", "--dest", str(link))
    assert r.returncode != 0 and "symlink" in r.stderr and list(real.iterdir()) == []


def test_retention_prunes_only_older_dumps_after_a_new_verified_one(b):
    b.dest.mkdir(mode=0o700)
    for i in range(5):
        (b.dest / f"memory-2026010{i}T000000.000000Z.dump").write_text("old")
    (b.dest / "notes.txt").write_text("not a backup")
    r = b.run("backup", "--dest", str(b.dest), "--keep", "3")
    assert r.returncode == 0
    names = sorted(p.name for p in b.dest.iterdir())
    assert len([n for n in names if n.endswith(".dump")]) == 3 and "notes.txt" in names
    assert "memory-20260100T000000.000000Z.dump" not in names
    r = b.run("backup", "--dest", str(b.dest), "--keep", "0")
    assert len(list(b.dest.glob("memory-*.dump"))) == 4          # --keep 0 keeps everything


def test_a_failed_backup_never_prunes(b):
    b.dest.mkdir(mode=0o700)
    for i in range(3):
        (b.dest / f"memory-2026010{i}T000000.000000Z.dump").write_text("old")
    assert b.run("backup", "--dest", str(b.dest), "--keep", "1", FAIL_DUMP="1").returncode == 1
    assert len(list(b.dest.glob("memory-*.dump"))) == 3


def test_restore_test_uses_and_drops_only_a_temporary_database(b):
    b.run("backup", "--dest", str(b.dest))
    r = b.run("restore-test", "--dest", str(b.dest))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["tables"] == 7 and out["temporary_database_dropped"] is True
    log = b.log.read_text()
    tmp = out and [ln for ln in log.splitlines() if "CREATE DATABASE" in ln][0].split('"')[1]
    assert tmp.startswith("restore_test_")
    assert f'DROP DATABASE IF EXISTS \\"{tmp}\\"' in log or f'DROP DATABASE IF EXISTS "{tmp}"' in log
    assert 'DROP DATABASE IF EXISTS "hindsight"' not in log and "DROP DATABASE hindsight" not in log
    assert f"pg_restore -U hindsight -d {tmp} --no-owner --exit-on-error" in log
    assert " -d hindsight --clean" not in log


@pytest.mark.parametrize("env,needle", [({"FAIL_RESTORE": "1"}, "restore into"), ({"TABLES": "0"}, "no tables")])
def test_restore_test_failures_still_drop_the_temporary_database(b, env, needle):
    b.run("backup", "--dest", str(b.dest))
    r = b.run("restore-test", "--dest", str(b.dest), **env)
    assert r.returncode == 1 and needle in r.stdout
    assert "DROP DATABASE IF EXISTS" in b.log.read_text()


def test_restore_test_without_a_backup_refuses(b):
    b.dest.mkdir(mode=0o700)
    r = b.run("restore-test", "--dest", str(b.dest))
    assert r.returncode == 1 and "no backup" in r.stdout and "CREATE DATABASE" not in b.log.read_text()


def test_age_reports_seconds_since_the_newest_backup(b):
    assert b.run("age", "--dest", str(b.dest)).returncode == 1
    b.run("backup", "--dest", str(b.dest))
    r = b.run("age", "--dest", str(b.dest))
    assert r.returncode == 0 and 0 <= int(r.stdout) < 60


def test_install_places_a_stable_copy_and_both_units_and_enables_the_timer(b):
    r = b.run("install")
    assert r.returncode == 0, r.stderr
    share = b.home / ".local/share/herdr-engineering/memory-backup.sh"
    assert share.is_file() and os.access(share, os.X_OK)
    u = b.home / ".config/systemd/user"
    assert (u / "herdr-hindsight-backup.service").read_text() == (CP / "herdr-hindsight-backup.service").read_text()
    assert (u / "herdr-hindsight-backup.timer").read_text() == (CP / "herdr-hindsight-backup.timer").read_text()
    log = b.log.read_text()
    assert "systemctl --user daemon-reload" in log and "systemctl --user enable --now herdr-hindsight-backup.timer" in log


def test_the_units_follow_the_repo_conventions():
    svc = (CP / "herdr-hindsight-backup.service").read_text()
    tmr = (CP / "herdr-hindsight-backup.timer").read_text()
    assert "Type=oneshot" in svc and "UMask=0077" in svc and "NoNewPrivileges=true" in svc
    assert "ExecStart=%h/.local/share/herdr-engineering/memory-backup.sh backup" in svc
    assert not [ln for ln in svc.splitlines() if ln.startswith(("Environment", "EnvironmentFile"))]                                   # YAML/default config only, no env
    assert "OnCalendar=daily" in tmr and "Persistent=true" in tmr and "WantedBy=timers.target" in tmr
    assert "Unit=herdr-hindsight-backup.service" in tmr


def test_the_script_never_reads_a_password():
    text = SCRIPT.read_text()
    assert "--no-password" in text and "PGPASSWORD" not in text and "HERDR_MEMORY_DB_PASSWORD" not in text


# ---- --target control-plane: the main database and its object store ----------------------------------------------

def test_control_plane_backup_dumps_the_database_and_the_object_store(b):
    r = b.run("backup", "--target", "control-plane", "--dest", str(b.dest))
    assert r.returncode == 0, r.stderr
    lines = [json.loads(ln) for ln in r.stdout.splitlines()]
    db, objects = Path(lines[0]["path"]), Path(lines[1]["path"])
    assert db.name.startswith("controlplane-") and db.suffix == ".dump" and db.read_bytes() == b"PGDMP-synthetic"
    assert objects.name.startswith("controlplane-objects-") and objects.suffix == ".tar"
    assert "data/bucket/payload" in subprocess.run(["tar", "-tf", str(objects)], capture_output=True, text=True).stdout
    assert stat.S_IMODE(objects.stat().st_mode) == 0o600 and list(b.dest.glob(".partial-*")) == []
    log = b.log.read_text()
    # the database user comes from the container's own environment; no password is read or passed
    assert "exec herdr-cp-postgres printenv POSTGRES_USER" in log
    assert "exec herdr-cp-postgres pg_dump -U cpadmin -d herdr --format=custom --no-password" in log
    assert "pg_restore --list" in log and "docker cp herdr-cp-rustfs:/data -" in log


def test_control_plane_backup_defaults_to_a_private_directory_under_the_stable_share(b):
    r = b.run("backup", "--target", "control-plane", "--no-objects")
    assert r.returncode == 0, r.stderr
    d = b.home / ".local/share/herdr-engineering/backups/control-plane"
    assert [p.name.split("-")[0] for p in d.iterdir()] == ["controlplane"] and stat.S_IMODE(d.stat().st_mode) == 0o700


def test_no_objects_takes_the_database_only(b):
    r = b.run("backup", "--target", "control-plane", "--dest", str(b.dest), "--no-objects")
    assert r.returncode == 0 and len(r.stdout.splitlines()) == 1
    assert "docker cp" not in b.log.read_text() and list(b.dest.glob("*.tar")) == []


def test_a_failed_object_store_copy_fails_the_run_but_keeps_the_good_database_dump(b):
    r = b.run("backup", "--target", "control-plane", "--dest", str(b.dest), FAIL_OBJECTS="1")
    assert r.returncode == 1 and "Object store" in r.stdout
    assert len(list(b.dest.glob("controlplane-*.dump"))) == 1
    assert list(b.dest.glob("*.tar")) == [] and list(b.dest.glob(".partial-*")) == []


def test_a_failed_control_plane_dump_exits_non_zero_and_leaves_nothing(b):
    r = b.run("backup", "--target", "control-plane", "--dest", str(b.dest), FAIL_DUMP="1")
    assert r.returncode == 1 and "error" in json.loads(r.stdout.splitlines()[0])
    assert list(b.dest.iterdir()) == []


def test_an_unreadable_container_user_is_an_error_not_a_default(b):
    r = b.run("backup", "--target", "control-plane", "--dest", str(b.dest), CP_USER="")
    assert r.returncode == 1 and "database user" in r.stdout and list(b.dest.iterdir()) == []


def test_prefix_names_a_separate_retention_series_and_never_prunes_another(b):
    b.dest.mkdir(mode=0o700)
    for i in range(3):
        (b.dest / f"controlplane-2026010{i}T000000.000000Z.dump").write_text("daily")
        (b.dest / f"predeploy-controlplane-2026010{i}T000000.000000Z.dump").write_text("old")
    r = b.run("backup", "--target", "control-plane", "--dest", str(b.dest), "--prefix", "predeploy-controlplane",
              "--keep", "2", "--no-objects")
    assert r.returncode == 0, r.stderr
    assert len(list(b.dest.glob("predeploy-controlplane-*.dump"))) == 2
    assert len(list(b.dest.glob("controlplane-2*.dump"))) == 3


def test_object_archives_are_pruned_by_the_same_keep_count(b):
    b.dest.mkdir(mode=0o700)
    for i in range(4):
        (b.dest / f"controlplane-objects-2026010{i}T000000.000000Z.tar").write_text("old")
    assert b.run("backup", "--target", "control-plane", "--dest", str(b.dest), "--keep", "2").returncode == 0
    assert len(list(b.dest.glob("controlplane-objects-*.tar"))) == 2


def test_control_plane_age_and_restore_test_use_the_control_plane_series(b):
    assert b.run("age", "--target", "control-plane", "--dest", str(b.dest)).returncode == 1
    b.run("backup", "--target", "control-plane", "--dest", str(b.dest))
    assert 0 <= int(b.run("age", "--target", "control-plane", "--dest", str(b.dest)).stdout) < 60
    r = b.run("restore-test", "--target", "control-plane", "--dest", str(b.dest))
    assert r.returncode == 0, r.stderr
    log = b.log.read_text()
    assert "exec -i herdr-cp-postgres pg_restore -U cpadmin -d restore_test_" in log
    assert 'DROP DATABASE IF EXISTS "herdr"' not in log


def test_control_plane_install_wires_its_own_units_and_leaves_the_memory_timer_alone(b):
    r = b.run("install", "--target", "control-plane")
    assert r.returncode == 0, r.stderr
    u = b.home / ".config/systemd/user"
    assert (u / "herdr-controlplane-backup.service").read_text() == (CP / "herdr-controlplane-backup.service").read_text()
    assert (u / "herdr-controlplane-backup.timer").read_text() == (CP / "herdr-controlplane-backup.timer").read_text()
    assert not (u / "herdr-hindsight-backup.timer").exists()
    log = b.log.read_text()
    assert "systemctl --user enable --now herdr-controlplane-backup.timer" in log and "hindsight" not in log


def test_an_unknown_target_is_refused(b):
    r = b.run("backup", "--target", "nope", "--dest", str(b.dest))
    assert r.returncode == 2 and "unknown target" in r.stderr and "docker" not in b.log.read_text()


def test_the_control_plane_units_follow_the_repo_conventions():
    svc = (CP / "herdr-controlplane-backup.service").read_text()
    tmr = (CP / "herdr-controlplane-backup.timer").read_text()
    assert "Type=oneshot" in svc and "UMask=0077" in svc and "NoNewPrivileges=true" in svc
    assert "ExecStart=%h/.local/share/herdr-engineering/memory-backup.sh backup --target control-plane" in svc
    assert not [ln for ln in svc.splitlines() if ln.startswith(("Environment", "EnvironmentFile"))]
    assert "OnCalendar=daily" in tmr and "Persistent=true" in tmr and "WantedBy=timers.target" in tmr
    assert "Unit=herdr-controlplane-backup.service" in tmr
