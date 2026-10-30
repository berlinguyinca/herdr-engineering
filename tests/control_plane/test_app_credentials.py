import os

import pytest

from herdr_engineering.control_plane.app_credentials import AppsError, CredentialStore, SecretBox


@pytest.fixture
def box(tmp_path):
    f = tmp_path / "key"
    f.write_bytes(os.urandom(32))
    return SecretBox(str(f))


def test_seal_roundtrip_and_ciphertext_hides_plaintext(box):
    ct = box.seal("s3cret-token")
    assert "s3cret" not in ct and box.open(ct) == "s3cret-token"


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
