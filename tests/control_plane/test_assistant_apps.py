import json
import os
import urllib.parse

import pytest

from herdr_engineering.control_plane.app_credentials import AppsError, CredentialStore, SecretBox
from herdr_engineering.control_plane.assistant_apps import AppsService
from herdr_engineering.control_plane.google_oauth import SCOPES

SECRET = "client-secret-xyz"
REFRESH = "refresh-tok-123"
ACCESS = "access-tok-456"


class Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode() if not isinstance(body, bytes) else body

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGoogle:
    def __init__(self):
        self.token = {"refresh_token": REFRESH, "access_token": ACCESS, "scope": " ".join(SCOPES)}
        self.calls = []
        self.fail_revoke = False

    def __call__(self, req, timeout):
        url = req.full_url
        self.calls.append((url, req.data))
        if url.endswith("/token"):
            return Resp(self.token)
        if url.endswith("/userinfo"):
            return Resp({"email": "ann@example.com", "email_verified": True})
        if url.endswith("/revoke"):
            if self.fail_revoke:
                raise OSError("down")
            return Resp(b"")
        raise AssertionError(url)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    f = tmp_path / "key"
    f.write_bytes(os.urandom(32))
    store = CredentialStore(SecretBox(str(f)))
    g, clock = FakeGoogle(), Clock()
    svc = AppsService(store, redirect_base="https://cp.example/", opener=g, clock=clock,
                      github_status_fn=lambda: {"connected": True, "account": "gh-bot", "status": "connected"})
    svc.save_client("google", "cid.apps", SECRET)
    return svc, store, g, clock


def _state(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]


def test_connect_start_url_has_four_scopes_pkce_and_state_and_no_secret(env):
    svc, *_ = env
    url = svc.connect_start("t", "u", "google")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert set(q["scope"][0].split()) == set(SCOPES) and len(SCOPES) == 4 and not any('gmail' in x for x in SCOPES)  # Gmail is restricted: not requested
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0] and q["state"][0]
    assert q["redirect_uri"] == ["https://cp.example/api/v1/assistant-apps/google/callback"]
    assert q["client_id"] == ["cid.apps"] and SECRET not in url


def test_callback_stores_connection_for_the_person_who_started_it(env):
    svc, store, g, _ = env
    st = _state(svc.connect_start("t1", "ann", "google"))
    assert svc.connect_callback("google", {"state": st, "code": "c0de"}) == ("t1", "ann")
    conn = store.get_connection("t1", "ann", "google")
    assert conn.account == "ann@example.com" and set(conn.scopes) == set(SCOPES)
    assert store.refresh_token("t1", "ann", "google") == REFRESH
    assert store.get_connection("t1", "bob", "google") is None
    sent = dict(urllib.parse.parse_qsl(g.calls[0][1].decode()))
    assert sent["code_verifier"] and sent["code"] == "c0de"


def test_callback_with_unknown_reused_expired_or_foreign_state_is_refused_and_stores_nothing(env):
    svc, store, g, clock = env
    with pytest.raises(AppsError) as e:
        svc.connect_callback("google", {"state": "nope", "code": "c"})
    assert e.value.reason == "bad_state" and not g.calls
    st = _state(svc.connect_start("t", "u", "google"))
    svc.connect_callback("google", {"state": st, "code": "c"})
    store.delete_connection("t", "u", "google")
    g.calls.clear()
    with pytest.raises(AppsError) as e:
        svc.connect_callback("google", {"state": st, "code": "c"})
    assert e.value.reason == "bad_state"
    st = _state(svc.connect_start("t", "u", "google"))
    clock.t += 601
    with pytest.raises(AppsError) as e:
        svc.connect_callback("google", {"state": st, "code": "c"})
    assert e.value.reason == "expired"
    with pytest.raises(AppsError) as e:       # also single use when it fails
        svc.connect_callback("google", {"state": st, "code": "c"})
    assert e.value.reason == "bad_state"
    st = _state(svc.connect_start("t", "u", "google"))
    with pytest.raises(AppsError) as e:       # foreign provider
        svc.connect_callback("github", {"state": st, "code": "c"})
    assert e.value.reason == "bad_state"
    assert store.get_connection("t", "u", "google") is None and not g.calls


def test_callback_refuses_a_token_with_extra_or_missing_scopes(env):
    svc, store, g, _ = env
    for scope in (" ".join(SCOPES) + " https://mail.google.com/", " ".join(SCOPES[:3]), ""):
        g.token["scope"] = scope
        st = _state(svc.connect_start("t", "u", "google"))
        with pytest.raises(AppsError) as e:
            svc.connect_callback("google", {"state": st, "code": "c"})
        assert e.value.reason == "unexpected_scope"
        n = len(g.calls)
        with pytest.raises(AppsError) as e:     # the state is spent
            svc.connect_callback("google", {"state": st, "code": "c"})
        assert e.value.reason == "bad_state" and len(g.calls) == n
    assert store.get_connection("t", "u", "google") is None


def test_denied_and_missing_refresh_token(env):
    svc, store, g, _ = env
    st = _state(svc.connect_start("t", "u", "google"))
    with pytest.raises(AppsError) as e:
        svc.connect_callback("google", {"state": st, "error": "access_denied"})
    assert e.value.reason == "denied"
    del g.token["refresh_token"]
    st = _state(svc.connect_start("t", "u", "google"))
    with pytest.raises(AppsError) as e:
        svc.connect_callback("google", {"state": st, "code": "c"})
    assert e.value.reason == "no_refresh_token"
    assert store.get_connection("t", "u", "google") is None


def test_disconnect_deletes_and_second_disconnect_is_quiet(env):
    svc, store, g, _ = env
    svc.connect_callback("google", {"state": _state(svc.connect_start("t", "u", "google")), "code": "c"})
    svc.disconnect("t", "u", "google")
    assert store.get_connection("t", "u", "google") is None
    revokes = [c for c in g.calls if c[0].endswith("/revoke")]
    assert len(revokes) == 1 and REFRESH in revokes[0][1].decode()
    svc.disconnect("t", "u", "google")
    assert len([c for c in g.calls if c[0].endswith("/revoke")]) == 1


def test_disconnect_survives_a_failing_revoke(env):
    svc, store, g, _ = env
    svc.connect_callback("google", {"state": _state(svc.connect_start("t", "u", "google")), "code": "c"})
    g.fail_revoke = True
    svc.disconnect("t", "u", "google")
    assert store.get_connection("t", "u", "google") is None


def test_providers_listing_never_contains_secrets(env):
    svc, *_ = env
    svc.connect_callback("google", {"state": _state(svc.connect_start("t", "u", "google")), "code": "c"})
    rows = svc.providers("t", "u")
    blob = json.dumps(rows)
    for s in (SECRET, REFRESH, ACCESS):
        assert s not in blob
    by = {r["id"]: r for r in rows}
    assert set(by) == {"gmail", "calendar", "github"}
    assert not by["gmail"]["connected"] and by["gmail"]["status"] == "not_connected"     # needs the restricted Gmail scopes
    assert by["calendar"]["connected"] and by["calendar"]["account"] == "ann@example.com" and by["calendar"]["client_configured"]
    assert not svc.providers("t", "other")[0]["connected"]


def test_github_status_provider_is_listed_read_only(env):
    svc, *_ = env
    gh = {r["id"]: r for r in svc.providers("t", "u")}["github"]
    assert gh["kind"] == "status" and gh["connected"] and gh["account"] == "gh-bot"


def test_save_client_refuses_empty_and_unknown(env):
    svc, *_ = env
    for args in (("google", "", "s"), ("google", "i", " "), ("slack", "i", "s")):
        with pytest.raises(AppsError):
            svc.save_client(*args)


def test_connect_start_needs_a_client(tmp_path):
    f = tmp_path / "k"
    f.write_bytes(os.urandom(32))
    svc = AppsService(CredentialStore(SecretBox(str(f))), redirect_base="https://x")
    with pytest.raises(AppsError) as e:
        svc.connect_start("t", "u", "google")
    assert e.value.reason == "no_client"


def test_blocked_calendars_roundtrip(env):
    svc, *_ = env
    svc.set_blocked_calendars("t", "u", ["a", "b", "a"])
    assert svc.blocked_calendars("t", "u") == ["a", "b"] and svc.blocked_calendars("t", "x") == []


def test_pending_sign_ins_are_capped_per_person(env):
    svc, store, g, _ = env
    first = _state(svc.connect_start("t", "u", "google"))
    for _ in range(5):
        svc.connect_start("t", "u", "google")
    with pytest.raises(AppsError) as e:
        svc.connect_callback("google", {"state": first, "code": "c"})
    assert e.value.reason == "bad_state"


def test_store_seal_pending_roundtrip_and_not_set_up(env, tmp_path):
    _, store, *_ = env
    assert store.open_pending(store.seal_pending("v")) == "v"
    with pytest.raises(AppsError) as e:
        CredentialStore(SecretBox(None)).seal_pending("v")
    assert e.value.reason == "not_set_up"
    with pytest.raises(AppsError) as e:
        store.open_pending("garbage")
    assert e.value.reason == "unreadable"


def test_callback_accepts_googles_short_form_of_the_email_scope(env):
    svc, store, g, _ = env
    g.token["scope"] = " ".join("email" if x.endswith("userinfo.email") else x for x in SCOPES)
    svc.connect_callback("google", {"state": _state(svc.connect_start("t", "u", "google")), "code": "c"})
    assert store.get_connection("t", "u", "google").account == "ann@example.com"
