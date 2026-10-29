"""A private, throwaway Postgres for integration tests: own data dir, unix socket only, deleted afterwards.

Never connects to anything that already exists. Skips (returns None) when no Postgres binaries are installed.
"""
import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path


def _bin(name):
    for d in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if (d / name).exists():
            return str(d / name)
    return shutil.which(name)


@contextlib.contextmanager
def private_postgres(*sql_files):
    initdb, pg_ctl = _bin("initdb"), _bin("pg_ctl")
    if not (initdb and pg_ctl and _bin("psql")):
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="pi-pg-") as tmp:
        data, sock = Path(tmp, "data"), Path(tmp, "sock")
        sock.mkdir()
        subprocess.run([initdb, "-D", str(data), "-A", "trust", "-U", "t"], check=True, capture_output=True)
        opts = f"-c listen_addresses= -c unix_socket_directories={sock} -c fsync=off"
        subprocess.run([pg_ctl, "-D", str(data), "-o", opts, "-w", "start", "-l", str(Path(tmp, "log"))], check=True, capture_output=True)
        try:
            for f in sql_files:
                subprocess.run([_bin("psql"), "-h", str(sock), "-U", "t", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-q", "-f", str(f)],
                               check=True, capture_output=True)
            yield f"postgresql://t@/postgres?host={sock}"
        finally:
            subprocess.run([pg_ctl, "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
