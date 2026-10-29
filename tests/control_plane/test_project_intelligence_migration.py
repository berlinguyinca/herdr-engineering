"""0020 migration: text checks always; real up/down/re-run against a throwaway Postgres when PI_TEST_PG_BIN is
not needed -- the integration test starts its own private server (marker: integration)."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SQL = Path(__file__).parents[2] / "deploy/control-plane/migrations/0020_project_intelligence.sql"
TABLES = ("pi_record", "pi_source_state", "pi_event", "pi_outbox", "pi_relationship", "pi_finding", "pi_audit")


def test_down_drops_exactly_the_tables_created_here():
    text = SQL.read_text()
    created = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", text)
    down = re.search(r"DROP TABLE IF EXISTS ([\w, ]+);", text).group(1)
    assert set(created) == set(TABLES) == {t.strip() for t in down.split(",")}
    assert "fabric_events" not in down and "artifact" not in down


def test_every_table_is_idempotent_and_scoped():
    text = SQL.read_text()
    assert text.count("CREATE TABLE IF NOT EXISTS") == len(TABLES)
    for table in set(TABLES) - {"pi_outbox"}:  # outbox is scoped through its event row
        body = text.split(f"CREATE TABLE IF NOT EXISTS {table} ", 1)[1].split(");", 1)[0]
        assert "org_id" in body, table


def _bin(name):
    for d in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if (d / name).exists():
            return str(d / name)
    return shutil.which(name)


@pytest.mark.integration
def test_up_rerun_down_on_private_postgres():
    initdb, pg_ctl, psql = _bin("initdb"), _bin("pg_ctl"), _bin("psql")
    if not (initdb and pg_ctl and psql):
        pytest.skip("postgres binaries not installed")
    with tempfile.TemporaryDirectory(prefix="pi-mig-") as tmp:
        data, sock = Path(tmp, "data"), Path(tmp, "sock")
        sock.mkdir()
        subprocess.run([initdb, "-D", str(data), "-A", "trust", "-U", "t"], check=True, capture_output=True)
        opts = f"-c listen_addresses= -c unix_socket_directories={sock} -c fsync=off"
        subprocess.run([pg_ctl, "-D", str(data), "-o", opts, "-w", "start", "-l", str(Path(tmp, "log"))],
                       check=True, capture_output=True)
        try:
            def run(sql=None, file=None):
                args = [psql, "-h", str(sock), "-U", "t", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-tA"]
                args += ["-f", file] if file else ["-c", sql]
                return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()
            run(file=str(SQL)); run(file=str(SQL))  # re-run is safe
            assert run("select count(*) from pg_tables where tablename like 'pi\\_%'") == str(len(TABLES))
            run("insert into pi_event(event_id,org_id,source_system,source_instance,source_id,event_type,"
                "occurred_at,object_ref,title,content_digest) values('e1','o','gmail','a','m','t',now(),'x','t','d')")
            with pytest.raises(subprocess.CalledProcessError):  # dedupe key is enforced by the database
                run("insert into pi_event(event_id,org_id,source_system,source_instance,source_id,event_type,"
                    "occurred_at,object_ref,title,content_digest) values('e2','o','gmail','a','m','t',now(),'x','t','d')")
            with pytest.raises(subprocess.CalledProcessError):  # a relationship needs evidence
                run("insert into pi_relationship(id,org_id,from_ref,to_ref,predicate,derivation,evidence_refs)"
                    " values('r','o','a','b','mentions','explicit','[]')")
            with pytest.raises(subprocess.CalledProcessError):  # inferred needs confidence + extractor
                run("insert into pi_relationship(id,org_id,from_ref,to_ref,predicate,derivation,evidence_refs)"
                    " values('r','o','a','b','mentions','inferred','[{\"a\":1}]')")
            down = re.search(r"DROP TABLE IF EXISTS [\w, ]+;", SQL.read_text()).group(0)
            run(down)
            assert run("select count(*) from pg_tables where tablename like 'pi\\_%'") == "0"
        finally:
            subprocess.run([pg_ctl, "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
