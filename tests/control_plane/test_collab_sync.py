"""File-synced Cognito directory: removal propagates, staleness fails closed."""
import json

import pytest

from herdr_engineering.control_plane.collab_cognito import CognitoAuthenticator, SyncedDirectory

T = [1000.0]


def write(d, members, updated):
    (d / "members.json").write_text(json.dumps({"updated": updated, "groups": {"local.dev": members}}))


@pytest.fixture
def auth(tmp_path):
    write(tmp_path, ["u1", "u2"], T[0])
    (tmp_path / "jwks.json").write_text(json.dumps({"keys": []}))
    d = SyncedDirectory(str(tmp_path), "local.dev", max_age=600, now=lambda: T[0])
    a = CognitoAuthenticator(pool_id="us-west-2_X", client_ids=["c"], required_group="local.dev",
                             jwks_fetch=d.jwks, group_lookup=d.groups, recheck_seconds=15,
                             now=lambda: T[0])
    return a, tmp_path


def test_member_in_group_and_removal_takes_effect_on_next_sync(auth):
    a, d = auth
    assert a.exists("u1") and a.exists("u2")
    write(d, ["u2"], T[0] + 20)
    T[0] += 20                      # past the recheck window
    assert not a.exists("u1") and a.exists("u2")


def test_stale_sync_admits_nobody_new_and_expires_known_users(auth):
    a, d = auth
    assert a.exists("u1")
    T[0] += 5000                    # the sync job stopped long ago
    assert not a.exists("u2")       # never checked before: fail closed
    assert not a.exists("u1")       # known user: grace window long gone


def test_non_member_and_missing_files_are_denied(auth):
    a, d = auth
    assert not a.exists("stranger")
    (d / "members.json").unlink()
    T[0] += 100
    assert not a.exists("u2")


def test_jwks_read_from_file(auth):
    a, _ = auth
    assert a._fetch() == {"keys": []}
