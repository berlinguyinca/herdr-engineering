import hashlib
import hmac
import json

from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane.project_intelligence_api import IntelligenceRoutes

SECRET = b"s3cret-for-test"
BODY = json.dumps({"action": "opened", "repository": {"full_name": "o/r"},
                   "pull_request": {"number": 3, "title": "t", "head": {"sha": "a" * 40}, "html_url": "https://github.com/o/r/pull/3",
                                    "user": {"login": "u"}, "updated_at": "2026-10-28T10:00:00Z", "state": "open"}}).encode()


def sign(body=BODY, secret=SECRET):
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def hdr(delivery="d1", event="pull_request", sig=None, body=BODY):
    return {"X-Hub-Signature-256": sig or sign(body), "X-GitHub-Delivery": delivery, "X-GitHub-Event": event}


def routes(tmp_path, **over):
    f = tmp_path / "secret"
    f.write_bytes(SECRET + b"\n")
    sec = {"enabled": True, "ingest": True, "default_org_id": "org", "webhook_secret_file": str(f),
           "webhook_projects": {"o/r": ["p1"]}, **over}
    return IntelligenceRoutes(pi.IntelligenceStore(), lambda: sec), sec


def test_unconfigured_is_404(tmp_path):
    r = IntelligenceRoutes(pi.IntelligenceStore(), lambda: {"enabled": True, "ingest": True})
    assert r.post_webhook(hdr(), BODY)[0] == 404
    r2, _ = routes(tmp_path, ingest=False)
    assert r2.post_webhook(hdr(), BODY)[0] == 404


def test_bad_or_missing_signature_is_401_and_stores_nothing(tmp_path):
    r, _ = routes(tmp_path)
    assert r.post_webhook(hdr(sig="sha256=" + "0" * 64), BODY)[0] == 401
    assert r.post_webhook({"X-GitHub-Delivery": "d"}, BODY)[0] == 401
    assert r.post_webhook(hdr(sig=sign(secret=b"other")), BODY)[0] == 401
    assert r.store.timeline(pi.Viewer("org", None, "restricted"), project_id="p1")["total"] == 0


def test_valid_delivery_is_stored_once_and_replay_is_duplicate(tmp_path):
    r, _ = routes(tmp_path)
    assert r.post_webhook(hdr(), BODY) == (202, {"status": "accepted"})
    assert r.post_webhook(hdr(), BODY) == (202, {"status": "duplicate"})
    assert r.store.timeline(pi.Viewer("org", None, "restricted"), project_id="p1")["total"] == 1


def test_unmapped_repo_and_ignored_event_are_acknowledged_not_stored(tmp_path):
    r, _ = routes(tmp_path, webhook_projects={})
    assert r.post_webhook(hdr(), BODY)[1] == {"status": "unmapped"}
    r2, _ = routes(tmp_path)
    ping = json.dumps({"repository": {"full_name": "o/r"}}).encode()
    assert r2.post_webhook(hdr(event="ping", body=ping), ping)[1] == {"status": "ignored"}


def test_kill_switch_and_missing_secret_file_fail_closed(tmp_path):
    r, _ = routes(tmp_path, kill_switch=True)
    assert r.post_webhook(hdr(), BODY)[0] == 503
    r2, _ = routes(tmp_path, webhook_secret_file=str(tmp_path / "nope"))
    assert r2.post_webhook(hdr(), BODY)[0] == 503


def test_oversize_and_bad_json(tmp_path):
    r, _ = routes(tmp_path)
    big = b"x" * 1_000_001
    assert r.post_webhook(hdr(body=big), big)[0] == 413
    junk = b"{not json"
    assert r.post_webhook(hdr(body=junk), junk)[0] == 400


# ---- through the real HTTP server -------------------------------------------------------------------------
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402


class _Fleet:
    def __init__(self, section):
        self._s = section

    def current(self):
        return self

    def section(self, name):
        return {"project_intelligence": self._s}


def _post(server, headers, body):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/intelligence/webhooks/github", data=body,
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as exc:
        r = exc
    with r:
        return r.status, json.load(r)


def _server(section):
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"op": "operator"}, fleet_config=_Fleet(section))
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    return server


def test_http_webhook_needs_no_api_key_but_needs_a_valid_signature(tmp_path):
    f = tmp_path / "secret"
    f.write_bytes(SECRET)
    server = _server({"enabled": True, "ingest": True, "default_org_id": "org", "webhook_secret_file": str(f),
                      "webhook_projects": {"o/r": ["p1"]}})
    try:
        assert _post(server, hdr(), BODY) == (202, {"status": "accepted"})
        assert _post(server, hdr(sig="sha256=" + "1" * 64, delivery="d2"), BODY)[0] == 401
        assert _post(server, hdr(), BODY)[1] == {"status": "duplicate"}
    finally:
        server.shutdown()


def test_http_webhook_is_404_when_the_feature_is_off():
    server = _server({})
    try:
        assert _post(server, hdr(), BODY)[0] == 404
    finally:
        server.shutdown()


def test_http_status_exposes_metrics_to_a_mapped_operator(tmp_path):
    import hashlib as _h
    digest = _h.sha256(b"op").hexdigest()[:12]
    server = _server({"enabled": True, "default_org_id": "org", "viewers": {f"key:{digest}": {"org_id": "org", "projects": "*"}}})
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/intelligence/status", headers={"X-API-Key": "op"})
        with urllib.request.urlopen(req, timeout=3) as r:
            body = json.load(r)
        assert body["enabled"] is True and set(body["metrics"]) == {"bridge", "queue_depth", "incidents"}
    finally:
        server.shutdown()


def test_reconcile_records_only_prs_no_webhook_saw_and_is_repeatable():
    from datetime import UTC, datetime
    from types import SimpleNamespace as NS

    from herdr_engineering.control_plane import intel_webhook as wh

    class App:
        def open_prs(self, repo):
            if repo == "bad/repo":
                raise RuntimeError("boom")
            return [NS(number=3, head_sha="a" * 40, author_login="u", base_ref="main"),
                    NS(number=4, head_sha="b" * 40, author_login="u", base_ref="main")]

    store, cfg = pi.IntelligenceStore(), {"enabled": True, "ingest": True}
    # PR 3 at that head was already delivered by a webhook
    store.append_event(wh.normalise("pull_request", json.loads(BODY), org_id="org", installation="github", project_ids=("p1",),
                                    received_at=datetime(2026, 10, 28, tzinfo=UTC)), cfg=cfg)
    args = dict(org_id="org", installation="github", repo_projects={"o/r": ["p1"], "bad/repo": ["p2"]},
                received_at=datetime(2026, 10, 28, 1, tzinfo=UTC))
    first = wh.reconcile_open_prs(App(), store, cfg, **args)
    assert first == {"seen": 2, "recorded": 1, "already": 1, "errors": 1}
    again = wh.reconcile_open_prs(App(), store, cfg, **args)
    assert again["recorded"] == 0
    assert store.timeline(pi.Viewer("org", None, "restricted"), project_id="p1")["total"] == 2
