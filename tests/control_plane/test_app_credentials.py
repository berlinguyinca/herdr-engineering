import os

import pytest

from herdr_engineering.control_plane.app_credentials import AppsError, CredentialStore, SecretBox


@pytest.fixture
def box(tmp_path):
    f = tmp_path / "key"
    f.write_bytes(os.urandom(32))
    return SecretBox(str(f))


def test_seal_roundtrip_and_ciphertext_hides_plaintext(box):
    import base64
    ct = box.seal("s3cret-token")
    assert box.seal("s3cret-token") != ct
    assert b"s3cret" not in base64.urlsafe_b64decode(ct) and "s3cret" not in ct
    assert box.open(ct) == "s3cret-token"


def test_tampered_or_foreign_ciphertext_is_unreadable(box, tmp_path):
    ct = box.seal("x")
    bad = ct[:-4] + ("AAAA" if not ct.endswith("AAAA") else "BBBB")
    other = tmp_path / "k2"
    other.write_bytes(os.urandom(32))
    for b, c in ((box, bad), (SecretBox(str(other)), ct)):
        with pytest.raises(AppsError) as e:
            b.open(c)
        assert e.value.reason == "unreadable"


def test_missing_or_short_key_file_is_not_available_and_save_is_refused(tmp_path):
    short = tmp_path / "short"
    short.write_bytes(b"x" * 8)
    for b in (SecretBox(None), SecretBox(str(tmp_path / "nope")), SecretBox(str(short))):
        assert not b.available
        with pytest.raises(AppsError) as e:
            CredentialStore(b).put_connection("t", "u", "gmail", account="a", scopes=[], refresh_token="r")
        assert e.value.reason == "not_set_up"
        with pytest.raises(AppsError) as e:
            b.open("x")
        assert e.value.reason == "not_set_up"


def test_connection_never_exposes_token_in_repr_or_as_dict(box):
    s = CredentialStore(box)
    s.put_connection("t", "u", "gmail", account="a@x", scopes=["m"], refresh_token="refresh-abc")
    conn = s.get_connection("t", "u", "gmail")
    assert "refresh" not in repr(conn) and "abc" not in repr(conn)
    assert "refresh" not in str(vars(conn)) and "abc" not in str(vars(conn))
    assert s.refresh_token("t", "u", "gmail") == "refresh-abc"
    s.set_status("t", "u", "gmail", "needs_attention")
    assert s.get_connection("t", "u", "gmail").status == "needs_attention"
    with pytest.raises(AppsError):
        s.set_status("t", "u", "gmail", "bogus")


def test_client_roundtrip(box):
    s = CredentialStore(box)
    s.put_client("google", "cid", "csecret")
    assert s.get_client("google") == ("cid", "csecret") and s.get_client("x") is None


def test_two_people_do_not_see_each_others_connections(box):
    s = CredentialStore(box)
    s.put_connection("t", "ann", "gmail", account="ann@x", scopes=[], refresh_token="ra")
    s.put_connection("t", "bob", "gmail", account="bob@x", scopes=[], refresh_token="rb")
    assert [c.account for c in s.list_connections("t", "ann")] == ["ann@x"]
    assert s.get_connection("t2", "ann", "gmail") is None
    assert s.refresh_token("t", "bob", "gmail") == "rb"
    s.delete_connection("t", "ann", "gmail")
    assert s.get_connection("t", "bob", "gmail") is not None


class _Fake:
    def __init__(self, fail=False):
        self.fail, self.conn_rows, self.blocked = fail, {}, {}

    def _check(self):
        if self.fail:
            raise RuntimeError("db down")

    def load_clients(self):
        return []

    def load_connections(self):
        return [(*k, a, __import__("json").dumps(sc), st, ct, cr) for k, (a, sc, st, ct, cr) in self.conn_rows.items()]

    def load_blocked(self):
        return [(*k, __import__("json").dumps(v)) for k, v in self.blocked.items()]

    def save_client(self, *a):
        self._check()

    def save_connection(self, t, u, p, account, scopes, status, ct, cr):
        self._check()
        self.conn_rows[(t, u, p)] = (account, scopes, status, ct, cr)

    def set_status(self, *a):
        self._check()

    def delete_connection(self, t, u, p):
        self._check()
        self.conn_rows.pop((t, u, p), None)

    def save_blocked(self, t, u, p, ids):
        self._check()
        self.blocked[(t, u, p)] = ids


def test_persistence_failure_does_not_lose_the_in_memory_connection(box):
    s = CredentialStore(box, _Fake(fail=True))
    s.put_connection("t", "u", "gmail", account="a", scopes=["m"], refresh_token="r1")
    assert s.refresh_token("t", "u", "gmail") == "r1"


def test_persistence_stores_only_ciphertext_and_reloads(box):
    p = _Fake()
    CredentialStore(box, p).put_connection("t", "u", "gmail", account="a", scopes=["m"], refresh_token="plain-r")
    assert "plain-r" not in repr(p.conn_rows)
    assert CredentialStore(box, p).refresh_token("t", "u", "gmail") == "plain-r"


def test_blocked_calendars_are_per_person_and_survive_delete_connection(box):
    p = _Fake()
    s = CredentialStore(box, p)
    s.put_connection("t", "ann", "calendar", account="a", scopes=[], refresh_token="r")
    s.set_blocked("t", "ann", "calendar", ["work", "work", "kids"])
    assert s.get_blocked("t", "ann", "calendar") == ["work", "kids"]
    assert s.get_blocked("t", "bob", "calendar") == []
    s.delete_connection("t", "ann", "calendar")
    assert s.get_blocked("t", "ann", "calendar") == ["work", "kids"]
    assert CredentialStore(box, p).get_blocked("t", "ann", "calendar") == ["work", "kids"]


def test_failed_delete_is_retried_and_never_resurrects_the_token(box, caplog):
    p = _Fake()
    s = CredentialStore(box, p)
    s.put_connection("t", "u", "gmail", account="a", scopes=[], refresh_token="r")
    p.fail = True
    with caplog.at_level("WARNING"):
        s.delete_connection("t", "u", "gmail")
    assert "delete not persisted" in caplog.text and ("t", "u", "gmail") in p.conn_rows
    assert s.get_connection("t", "u", "gmail") is None
    p.fail = False
    s.set_blocked("t", "u", "gmail", ["c"])           # the next write retries the delete
    assert ("t", "u", "gmail") not in p.conn_rows
    assert CredentialStore(box, p).get_connection("t", "u", "gmail") is None


def test_reconnect_after_failed_delete_cancels_the_tombstone(box):
    p = _Fake()
    s = CredentialStore(box, p)
    s.put_connection("t", "u", "gmail", account="a", scopes=[], refresh_token="r1")
    p.fail = True
    s.delete_connection("t", "u", "gmail")
    p.fail = False
    s.put_connection("t", "u", "gmail", account="a", scopes=[], refresh_token="r2")
    assert s.refresh_token("t", "u", "gmail") == "r2" and ("t", "u", "gmail") in p.conn_rows


def test_malformed_saved_row_is_skipped_alone(box):
    p = _Fake()
    CredentialStore(box, p).put_connection("t", "ok", "gmail", account="a", scopes=[], refresh_token="r")
    p.conn_rows[("t", "bad", "gmail")] = ("a", 5, "connected", "ct", "now")
    s = CredentialStore(box, p)
    assert s.get_connection("t", "ok", "gmail") and s.get_connection("t", "bad", "gmail") is None


class _Cur:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args):
        self.log.append(("execute", sql, args))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, log, rows=()):
        self.log, self.rows = log, list(rows)

    def cursor(self):
        return _Cur(self.log, self.rows)

    def commit(self):
        self.log.append(("commit",))

    def close(self):
        self.log.append(("close",))


def test_postgres_persistence_upserts_commits_closes_and_loads():
    from herdr_engineering.control_plane.app_credentials import PostgresCredentialPersistence
    log = []
    p = PostgresCredentialPersistence("postgres://user:pw@h/db", connect=lambda: _Conn(log, [("t", "u", "gmail", "a", "[]", "connected", "ct", "now")]))
    p.save_connection("t", "u", "gmail", "a", ["m"], "connected", "ct", "now")
    kinds = [e[0] for e in log]
    assert kinds == ["execute", "commit", "close"]
    sql, args = log[0][1], log[0][2]
    assert "ON CONFLICT (tenant_id, user_id, provider) DO UPDATE" in sql and "%s::jsonb" in sql
    assert args[4] == '["m"]' and "pw" not in repr(args)
    p.save_blocked("t", "u", "calendar", ["x"])
    assert "assistant_app_blocked" in log[3][1] and "::jsonb" in log[3][1]
    log.clear()
    assert p.load_connections()[0][0] == "t" and [e[0] for e in log] == ["execute", "commit", "close"]


def test_created_at_from_database_is_normalised_to_utc(box):
    from datetime import datetime, timedelta, timezone
    p = _Fake()
    p.conn_rows[("t", "u", "gmail")] = ("a", [], "connected", box.seal("r"),
                                        datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=-8))))
    s = CredentialStore(box, p)
    assert s.get_connection("t", "u", "gmail").created_at == "2026-01-01T20:00:00+00:00"


def test_no_tombstone_without_persistence(box):
    s = CredentialStore(box)
    s.put_connection("t", "u", "gmail", account="a", scopes=[], refresh_token="r")
    s.delete_connection("t", "u", "gmail")
    assert s._tombstones == set()
