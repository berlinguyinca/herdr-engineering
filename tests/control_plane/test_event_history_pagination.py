import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def populate(repo, count=137, entity_type="mission", entity_id="pi:MSN-history"):
    rows = [{"event_id": f"event-{i}", "entity_type": entity_type, "entity_id": entity_id,
             "sequence": (i // 3) * 7, "event_type": "Observed",
             "payload": {"index": i, "detail": "private payload do not expose in cursor"}}
            for i in range(count)]
    with repo._event_lock:
        repo._events[(entity_type, entity_id)] = rows
    return rows


def test_all_pages_preserve_duplicate_sequences_gaps_and_appends():
    repo = ControlPlaneRepo()
    original = list(populate(repo))
    page = repo.stream_page("mission", "pi:MSN-history", limit=17)
    assert page["events"] == original[-17:]
    assert page["latest_sequence"] == original[-1]["sequence"]
    assert page["last_event_id"] == original[-1]["event_id"]
    pages = [page["events"]]
    cursor = page["next_cursor"]
    assert len(cursor) <= 256
    decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    assert "private payload" not in decoded and "pi:MSN-history" not in decoded
    appended = repo.record_event("mission", "pi:MSN-history", "NewEvent", {"new": True})
    while page["has_more"]:
        page = repo.stream_page("mission", "pi:MSN-history", limit=17, before=page["next_cursor"])
        pages.append(page["events"])
        assert page["latest_sequence"] == appended["sequence"]
        assert page["last_event_id"] == appended["event_id"]
    assert page["next_cursor"] is None
    assert [event for group in reversed(pages) for event in group] == original
    assert repo.stream_page("mission", "pi:MSN-history", limit=17, before=cursor)["events"] == pages[1]
    assert repo.stream_page("mission", "pi:MSN-history", limit=1)["events"] == [appended]
    assert repo.stream("mission", "pi:MSN-history") == original + [appended]


def test_default_and_empty_pages():
    repo = ControlPlaneRepo()
    rows = populate(repo, 70)
    assert repo.stream_page("mission", "pi:MSN-history")["events"] == rows[-50:]
    assert repo.stream_page("mission", "unknown") == {
        "events": [], "has_more": False, "next_cursor": None,
        "latest_sequence": None, "last_event_id": None,
    }


def test_append_with_duplicate_sequence_updates_banner_identity_on_older_page():
    repo = ControlPlaneRepo()
    rows = populate(repo, 10)
    cursor = repo.stream_page("mission", "pi:MSN-history", limit=3)["next_cursor"]
    older = repo.stream_page("mission", "pi:MSN-history", limit=3, before=cursor)
    rows.append({**rows[-1], "event_id": "new-event-same-sequence"})
    refreshed = repo.stream_page("mission", "pi:MSN-history", limit=3, before=cursor)
    assert refreshed["events"] == older["events"]
    assert refreshed["latest_sequence"] == older["latest_sequence"]
    assert refreshed["last_event_id"] == "new-event-same-sequence"


@pytest.mark.parametrize("index", [-1, 0, True, 1.5, "7", 2**63])
def test_cursor_boundary_must_be_a_bounded_positive_integer(index):
    repo = ControlPlaneRepo()
    populate(repo, 10)
    cursor = repo.stream_page("mission", "pi:MSN-history", limit=3)["next_cursor"]
    content = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    content[1] = index
    malformed = base64.urlsafe_b64encode(json.dumps(content).encode()).decode().rstrip("=")
    with pytest.raises(ValueError, match="invalid history cursor"):
        repo.stream_page("mission", "pi:MSN-history", before=malformed)


@pytest.mark.parametrize("limit", [0, -1, 201, True, None, 1.5, "50"])
def test_repo_rejects_invalid_page_limits(limit):
    with pytest.raises(ValueError, match="limit"):
        ControlPlaneRepo().stream_page("mission", "m1", limit=limit)


@pytest.mark.parametrize("cursor", ["", "!", "a" * 257, "not/a/cursor", 3, True,
                                    base64.urlsafe_b64encode(b'{}').decode().rstrip("=")])
def test_repo_rejects_invalid_cursors(cursor):
    with pytest.raises(ValueError, match="cursor"):
        ControlPlaneRepo().stream_page("mission", "m1", before=cursor)


@pytest.mark.parametrize("mutation", ["prune", "clear", "replace_anchor"])
def test_changed_boundary_returns_stale_cursor(mutation):
    repo = ControlPlaneRepo()
    rows = populate(repo, 10)
    cursor = repo.stream_page("mission", "pi:MSN-history", limit=3)["next_cursor"]
    if mutation == "prune":
        del rows[:1]
    elif mutation == "clear":
        rows.clear()
    else:
        rows[7] = {**rows[7], "payload": {"changed": True}}
    with pytest.raises(ValueError, match="^staleHistoryCursor$"):
        repo.stream_page("mission", "pi:MSN-history", before=cursor)


@pytest.mark.parametrize("entity_type,entity_id", [("session", "pi:MSN-history"), ("mission", "other:MSN-history")])
def test_cursor_cannot_be_retargeted_to_another_typed_entity(entity_type, entity_id):
    repo = ControlPlaneRepo()
    rows = populate(repo, 10)
    cursor = repo.stream_page("mission", "pi:MSN-history", limit=3)["next_cursor"]
    # Even a byte-identical stream at another route cannot accept this cursor.
    repo._events[(entity_type, entity_id)] = list(rows)
    del rows[:1]
    with pytest.raises(ValueError, match="cursor"):
        repo.stream_page(entity_type, entity_id, before=cursor)


def test_page_reads_only_bounded_slice_and_anchor_not_full_history():
    class BoundedReads(list):
        def __iter__(self):
            raise AssertionError("pagination must not iterate the full stream")

        def __getitem__(self, key):
            if isinstance(key, slice):
                assert key.stop - key.start <= 50
            return super().__getitem__(key)

    repo = ControlPlaneRepo()
    rows = populate(repo, 10001)
    repo._events[("mission", "pi:MSN-history")] = BoundedReads(rows)
    first = repo.stream_page("mission", "pi:MSN-history")
    assert first["events"] == rows[-50:]
    assert repo.stream_page("mission", "pi:MSN-history", before=first["next_cursor"])["events"] == rows[-100:-50]


@pytest.fixture
def history_api():
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, api_keys={"reader": "viewer"})
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()

    def get(query):
        request = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/events?{query}",
                                         headers={"X-API-Key": "reader"})
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    try:
        yield repo, get
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_api_legacy_contract_and_opt_in_pages_with_namespaced_ids(history_api):
    repo, get = history_api
    rows = populate(repo)
    query = urllib.parse.urlencode({"entity_type": "mission", "entity_id": "pi:MSN-history"})
    assert get(query) == (200, {"events": rows})
    status, latest = get(query + "&limit=20")
    assert status == 200 and latest["events"] == rows[-20:]
    assert latest["has_more"] and latest["last_event_id"] == rows[-1]["event_id"]
    status, older = get(query + "&" + urllib.parse.urlencode({"before": latest["next_cursor"]}))
    assert status == 200 and older["events"] == rows[-70:-20]
    appended = repo.record_event("mission", "pi:MSN-history", "new", {"new": True})
    status, refreshed = get(query + "&" + urllib.parse.urlencode({"before": latest["next_cursor"]}))
    assert status == 200 and refreshed["events"] == older["events"]
    assert refreshed["last_event_id"] == appended["event_id"]
    assert get(query + "&unrelated=ignored") == (200, {"events": rows})
    del rows[:1]
    assert get(query + "&" + urllib.parse.urlencode({"before": latest["next_cursor"]})) == (
        409, {"error": "staleHistoryCursor"})


def test_api_rejects_invalid_new_parameters_and_keeps_private_stream_denials(history_api):
    _, get = history_api
    query = "entity_type=mission&entity_id=m1"
    for suffix in ("limit=", "limit=0", "limit=201", "limit=-2", "limit=1.5", "limit=abc",
                   "limit=2&limit=3", "before=", "before=!", "before=" + "a" * 257,
                   "before=a&before=b"):
        status, body = get(query + "&" + suffix)
        assert status == 400 and "error" in body, suffix
    for entity_type in ("collab", "collab_room", "lineage", "review_request"):
        for suffix in ("", "&limit=50", "&before=invalid"):
            assert get(f"entity_type={entity_type}&entity_id=private{suffix}") == (404, {"error": "not found"})
