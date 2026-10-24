"""Joining LLM usage rows to dashboard sessions (LLM page design, Decision 4).

A usage row's session_id comes from X-InferWeave-Session-ID / X-Session-Id or the key
label ``session:<id>``, all limited to ``[A-Za-z0-9-_.:/]{1,128}``. The dashboard id is
``herdr:<host>:<server>:<pane>`` with each part encodeURIComponent-ed (emit.js), so a
pane ``w1:p1`` becomes ``w1%3Ap1``, and ``%`` can never travel in a header. The join
therefore accepts: the dashboard id itself, its decoded form, and pi's native session
id (what pi sends as X-InferWeave-Session-ID), found through the session record's
``agent_session``. Anything else is labelled unmatched, never guessed.
"""
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.llm_session_id import dashboard_form, header_form

DASH = "herdr:beast:eng:w1%3Ap1"          # the literal the feed plugin emits (enrich.test.js)
PI_ID = "0199f2c4-6a1e-7b3c-9d2e-4f5a6b7c8d9e"
PI_FILE = f"/home/u/.pi/agent/sessions/--home-u-proj--/2026-10-23T10-00-00-000Z_{PI_ID}.jsonl"


def _repo():
    repo = ControlPlaneRepo()
    repo.upsert_session(DASH, host_id="beast", pane_id="w1:p1", herdr_session="eng",
                        agent_role="pi", agent_session={"agent": "pi", "kind": "path",
                                                        "value": PI_FILE})
    repo.upsert_session("herdr:bender:default:w2%3Ap3", host_id="bender", pane_id="w2:p3",
                        herdr_session="default", agent_role="claude",
                        agent_session={"agent": "claude", "kind": "id",
                                       "value": "c1a2b3c4-d5e6-4f70-8a9b-0c1d2e3f4a5b"})
    return repo


def test_header_form_round_trips_the_dashboard_id():
    assert header_form(DASH) == "herdr:beast:eng:w1:p1"
    assert dashboard_form("herdr:beast:eng:w1:p1") == DASH
    assert dashboard_form(header_form(DASH)) == DASH
    # a pane id with characters encodeURIComponent keeps as-is
    assert dashboard_form("herdr:h-1.lan:srv_2.x:w_1-a.b") == "herdr:h-1.lan:srv_2.x:w_1-a.b"
    assert dashboard_form("herdr:h:s:a/b") == "herdr:h:s:a%2Fb"


def test_non_dashboard_values_have_no_dashboard_form():
    for value in (None, "", "s1", "herdr:only:three", "herdr::eng:w1:p1", "herdr:h:s:"):
        assert dashboard_form(value) is None
    assert header_form("s1") is None
    assert header_form("herdr:beast:eng:w1%ZZ") is None   # broken encoding


def test_resolve_direct_decoded_and_native():
    repo = _repo()
    assert repo.resolve_usage_session(DASH) == {"session_id": DASH, "match": "direct"}
    assert repo.resolve_usage_session("herdr:beast:eng:w1:p1") == {"session_id": DASH,
                                                                    "match": "decoded"}
    assert repo.resolve_usage_session(PI_ID) == {"session_id": DASH, "match": "agent_session"}
    assert repo.resolve_usage_session("c1a2b3c4-d5e6-4f70-8a9b-0c1d2e3f4a5b") == {
        "session_id": "herdr:bender:default:w2%3Ap3", "match": "agent_session"}


def test_resolve_never_guesses():
    repo = _repo()
    assert repo.resolve_usage_session(None) == {"session_id": None, "match": "none"}
    assert repo.resolve_usage_session("proj:llmkey_x") == {"session_id": None, "match": "unmatched"}
    # a fragment of the native id is not the id
    assert repo.resolve_usage_session(PI_ID[-12:]) == {"session_id": None, "match": "unmatched"}
    # two records claiming the same native id: ambiguous, so unmatched
    repo.upsert_session("herdr:beast:eng:w9%3Ap9", host_id="beast", pane_id="w9:p9",
                        herdr_session="eng", agent_role="pi",
                        agent_session={"agent": "pi", "kind": "id", "value": PI_ID})
    assert repo.resolve_usage_session(PI_ID) == {"session_id": None, "match": "unmatched"}


def test_token_analytics_groups_by_dashboard_session_and_labels_unmatched():
    repo = _repo()
    for sid in (DASH, "herdr:beast:eng:w1:p1", PI_ID, "someone-else", None):
        repo.record_token_usage_row({"schema": 2, "project": "p", "model": "m", "status": 200,
                                     "input_tokens": 1, "output_tokens": 2, "session_id": sid})
    out = repo.token_analytics()
    assert out["total"]["requests"] == 5
    by = out["by_session"]
    assert set(by) == {DASH, "unmatched:someone-else"}
    # The encoded id itself fails the usage-row whitelist ('%'), so that row carries no
    # session: only the decoded form and the native id reach a session.
    assert by[DASH]["requests"] == 2 and by[DASH]["output_tokens"] == 4
    assert by[DASH]["session_id"] == DASH
    assert by[DASH]["matched_by"] == ["agent_session", "decoded"]
    un = by["unmatched:someone-else"]
    assert un["session_id"] is None and un["matched_by"] == ["unmatched"]
    assert un["raw_session_ids"] == ["someone-else"] and un["requests"] == 1
    # the raw id on the stored record is never rewritten
    raw = [e["payload"]["session_id"] for e in repo.stream("token_usage", "p")]
    assert raw == [None, "herdr:beast:eng:w1:p1", PI_ID, "someone-else", None]


def test_direct_match_for_ids_that_fit_the_charset():
    repo = _repo()
    repo.upsert_session("s1", agent_role="pi")       # e.g. a `herdr-eng session run --session s1`
    repo.record_token_usage_row({"schema": 2, "project": "p", "model": "m", "status": 200,
                                 "session_id": "s1"})
    assert repo.token_analytics()["by_session"]["s1"]["matched_by"] == ["direct"]


def test_v1_rows_join_through_the_envelope_session():
    repo = _repo()
    repo.record_token_usage("p", "m", 1, 1, session_id=PI_ID)   # v1: no session in the payload
    assert repo.token_analytics()["by_session"][DASH]["matched_by"] == ["agent_session"]
