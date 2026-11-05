# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Google connect from the Rakazo app: the Integrations screen's connections/* procs drive the existing AppsService."""
import os

import pytest

from herdr_engineering.control_plane.app_credentials import CredentialStore, SecretBox
from herdr_engineering.control_plane.assistant_apps import AppsService
from tests.control_plane.test_rakazo_bridge import alice, make_server, srv  # noqa: F401
from tests.control_plane.test_rakazo_contract import validate


@pytest.fixture
def apps(srv, tmp_path):
    server, _ = srv
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    svc = AppsService(CredentialStore(SecretBox(str(key))), redirect_base="https://cp.example")
    svc.save_client("google", "client-id", "client-secret")
    server.rakazo.apps_fn = lambda: svc
    return svc


def person(server):
    bridge = server.rakazo
    user = next(iter(bridge._by_principal))
    return bridge.canonical_person(bridge.svc.tenant_for(user), user)


def test_tools_is_empty(srv, alice, apps):
    _, api = srv
    tools = api.ok("connections/tools", {"connectorId": "google", "provider": "gmail"}, alice)
    validate("connections/tools", tools)
    assert tools == []


def test_catalog_lists_gmail_and_calendar(srv, alice, apps):
    _, api = srv
    rows = api.ok("connections/catalog", {}, alice)
    validate("connections/catalog", rows)
    assert [(r["slug"], r["connected"]) for r in rows] == [("gmail", False), ("calendar", False)]
    assert [r["slug"] for r in api.ok("connections/catalog", {"query": "cal"}, alice)] == ["calendar"]


def test_begin_returns_the_google_url_and_complete_waits_until_connected(srv, alice, apps):
    server, api = srv
    began = api.ok("connections/begin", {"connectorId": "google", "provider": "gmail", "displayName": "Gmail"}, alice)
    validate("connections/begin", began)
    assert began["authorizationUrl"].startswith("https://accounts.google.com/")
    pending = api.ok("connections/complete", {"connectionId": began["connectionId"]}, alice)
    validate("connections/complete", pending)
    assert pending["status"] == "pending"
    tenant, user = person(server)
    from herdr_engineering.control_plane import google_oauth
    apps._store.put_connection(tenant, user, "google", account="me@example.com",
                               scopes=sorted(google_oauth.GMAIL_SCOPES), refresh_token="r")
    done = api.ok("connections/complete", {"connectionId": began["connectionId"]}, alice)
    validate("connections/complete", done)
    assert (done["status"], done["displayName"], done["provider"]) == ("connected", "me@example.com", "gmail")
    rows = api.ok("connections/list", {}, alice)
    validate("connections/list", rows)
    assert [r["id"] for r in rows] == [began["connectionId"], "google:calendar"]    # one sign-in, both cards
    assert api.ok("connections/catalog", {}, alice)[0]["connected"] is True


def test_revoke_disconnects_and_unknown_ids_are_refused(srv, alice, apps):
    server, api = srv
    tenant, user = person(server)
    from herdr_engineering.control_plane import google_oauth
    apps._store.put_connection(tenant, user, "google", account="me@example.com",
                               scopes=sorted(google_oauth.GMAIL_SCOPES), refresh_token="r")
    revoked = api.ok("connections/revoke", {"connectionId": "google:gmail"}, alice)
    validate("connections/revoke", revoked)
    assert revoked == {"ok": True}
    assert api.ok("connections/list", {}, alice) == []
    assert api.rpc("connections/revoke", {"connectionId": "composio:x"}, alice)[0] == 404
    assert api.rpc("connections/begin", {"connectorId": "composio", "provider": "slack", "displayName": "S"}, alice)[0] == 400
    assert api.rpc("connections/rename", {"connectionId": "google:gmail", "displayName": "x"}, alice)[0] == 400


def test_without_apps_the_screen_is_empty_and_begin_explains(srv, alice):
    _, api = srv
    assert api.ok("connections/catalog", {}, alice) == []
    assert api.ok("connections/list", {}, alice) == []
    status, body = api.rpc("connections/begin", {"connectorId": "google", "provider": "gmail", "displayName": "G"}, alice)[:2]
    assert status == 400


def test_begin_without_a_google_client_says_the_operator_must_set_it_up(srv, alice, tmp_path):
    server, api = srv
    key = tmp_path / "k2"
    key.write_bytes(os.urandom(32))
    server.rakazo.apps_fn = lambda: AppsService(CredentialStore(SecretBox(str(key))), redirect_base="https://cp.example")
    status = api.rpc("connections/begin", {"connectorId": "google", "provider": "calendar", "displayName": "C"}, alice)[0]
    assert status == 400
