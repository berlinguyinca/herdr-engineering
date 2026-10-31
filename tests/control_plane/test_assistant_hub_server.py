"""Hub listener over REAL loopback sockets. No mocks: a real HubServer, a real WorkerRegistry and AssistantService."""
import json
import socket
import time
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.assistant_hub.events import list_events
from herdr_engineering.control_plane.assistant_hub.mcp import Hub
from herdr_engineering.control_plane.assistant_hub.server import HubServer
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry

LOOP = ["127.0.0.0/8"]
PING = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}'


def make_hub(enabled=True):
    cfg = assistant_config({"personal_assistant": {"enabled": enabled, "actions": {"allow_self_approval": True}}})
    svc = AssistantService(config=cfg)
    reg = WorkerRegistry(svc.store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30),
                         now=lambda: datetime(2026, 10, 30, 12, 0, tzinfo=UTC))
    return Hub(registry=reg, service=svc, store=svc.store)


@pytest.fixture
def started():
    servers = []

    def _start(hub=None, **kw):
        hub = hub or make_hub()
        kw.setdefault("allowed_networks", LOOP)
        kw.setdefault("allowed_hosts", [])
        srv = HubServer(hub, host="127.0.0.1", port=0, **kw)
        srv.start()
        srv.wait_until_ready()
        servers.append(srv)
        return hub, srv

    yield _start
    for s in servers:
        s.shutdown()


def raw(port, data: bytes):
    """Send bytes, read to EOF, return (status, headers dict lowercased, body bytes)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(data)
        chunks = []
        while True:
            try:
                c = s.recv(65536)
            except ConnectionResetError:
                break
            if not c:
                break
            chunks.append(c)
    blob = b"".join(chunks)
    head, _, body = blob.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {k.lower(): v for k, v in (ln.split(": ", 1) for ln in lines[1:] if ": " in ln)}
    return status, headers, body


def req(port, method="POST", path="/assistant/mcp", body=b"", headers=None, host=None, ctype="application/json",
        length=True):
    h = {"Host": host or f"127.0.0.1:{port}"}
    if ctype:
        h["Content-Type"] = ctype
    if length:
        h["Content-Length"] = str(len(body))
    h.update(headers or {})
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
    return raw(port, head.encode() + body)


def jpost(port, path, obj, **kw):
    return req(port, path=path, body=json.dumps(obj).encode(), **kw)


def enrol(hub, port, device="dev1"):
    _, code = hub.registry.create_enrollment(tenant_id="default", user_id="u1", name="box", harness="pi")
    st, _, body = jpost(port, "/assistant/enroll", {"code": code, "device_id": device})
    assert st == 200, body
    return json.loads(body), code


def mcp(port, cred, device, method, params=None):
    msg = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        msg["params"] = params
    return jpost(port, "/assistant/mcp", msg,
                 headers={"Authorization": f"Worker {cred}", "X-Herdr-Device": device})


def test_refuses_wildcard_bind_and_public_ip():
    hub = make_hub()
    for host in ("0.0.0.0", "::", "8.8.8.8", "", "localhost"):
        with pytest.raises(ValueError):
            HubServer(hub, host=host, port=0, allowed_networks=LOOP + ["100.64.0.0/10"], allowed_hosts=[])
    with pytest.raises(ValueError):
        HubServer(hub, host="127.0.0.1", port=0, allowed_networks=["100.64.0.0/10"], allowed_hosts=[])


def test_mcp_roundtrip_with_enrolled_worker(started):
    hub, srv = started()
    creds, _ = enrol(hub, srv.port)
    assert set(creds) == {"credential", "worker_id", "device_id", "expires_at"}
    st, headers, body = mcp(srv.port, creds["credential"], "dev1", "initialize")
    assert st == 200
    assert json.loads(body)["result"]["serverInfo"]["name"] == "herdr-assistant"
    assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"
    assert int(headers["content-length"]) == len(body)
    st, _, body = mcp(srv.port, creds["credential"], "dev1", "tools/list")
    assert st == 200 and any(t["name"] == "report" for t in json.loads(body)["result"]["tools"])
    st, _, body = mcp(srv.port, creds["credential"], "other-device", "tools/list")
    assert st == 401
    with socket.create_connection(("127.0.0.1", srv.port), timeout=5):
        pass                                      # a connection that sends nothing must not break the server
    st, _, _ = mcp(srv.port, creds["credential"], "dev1", "tools/list")
    assert st == 200


def test_notification_returns_202_empty(started):
    hub, srv = started()
    creds, _ = enrol(hub, srv.port)
    msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    st, _, body = jpost(srv.port, "/assistant/mcp", msg, headers={
        "Authorization": f"Worker {creds['credential']}", "X-Herdr-Device": "dev1"})
    assert st == 202 and body == b""


def test_peer_outside_allowed_networks_rejected(started):
    hub, srv = started(peer_networks=["127.0.0.2/32"])
    # invalid JSON body: if the body were parsed we would see 400, never 403
    st, headers, body = req(srv.port, body=b"{not json")
    assert st == 403
    assert headers["cache-control"] == "no-store"
    assert b"json" not in body.lower() and b"127.0.0.1" not in body
    st2, _, body2 = req(srv.port, method="GET", path="/nope")
    assert (st2, body2) == (403, body)


def test_x_forwarded_for_ignored(started):
    _, srv = started(peer_networks=["127.0.0.2/32"])
    for h in ({"X-Forwarded-For": "127.0.0.2"}, {"Forwarded": "for=127.0.0.2"}, {"X-Real-IP": "127.0.0.2"}):
        st, _, _ = req(srv.port, body=b"{}", headers=h)
        assert st == 403


def test_bad_host_header_rejected(started):
    _, srv = started()
    for host in ("evil.example.com", "evil.example.com:80", "100.64.1.2"):
        st, _, body = req(srv.port, body=b"{}", host=host)
        assert st == 403 and b"evil" not in body
    st, _, _ = req(srv.port, body=PING, host="127.0.0.1")
    assert st == 401
    _, srv2 = started(allowed_hosts=["box.tail1234.ts.net"])
    st, _, _ = req(srv2.port, body=PING, host="box.tail1234.ts.net")
    assert st == 401


def test_origin_rejected(started):
    _, srv = started()
    st, _, _ = req(srv.port, body=b"{}", headers={"Origin": "http://evil.example"})
    assert st == 403


def test_ambiguous_framing_rejected(started):
    _, srv = started()
    base = (b"POST /assistant/mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n")
    st, _, _ = raw(srv.port, base + b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
    assert st == 400
    st, _, _ = raw(srv.port, base + b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}")
    assert st == 400
    st, _, _ = raw(srv.port, base + b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n")
    assert st == 400
    st, _, _ = raw(srv.port, b"GET /assistant/mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2\r\n\r\n{}")
    assert st == 400


def test_oversize_body_413(started):
    _, srv = started(max_body=100)
    st, _, _ = req(srv.port, body=b"x" * 101)
    assert st == 413
    st, _, _ = req(srv.port, body=b"", length=True)
    assert st == 413
    st, _, _ = req(srv.port, body=b"{}", length=False)
    assert st == 413
    st, _, _ = req(srv.port, body=b"{}", headers={"Content-Length": "abc"}, length=False)
    assert st == 413


def test_wrong_content_type_415(started):
    _, srv = started()
    assert req(srv.port, body=b"{}", ctype="text/plain")[0] == 415
    assert req(srv.port, body=b"{}", ctype=None)[0] == 415
    assert req(srv.port, body=b"{}", ctype="application/json; charset=utf-8")[0] == 400
    assert req(srv.port, body=PING, ctype="Application/JSON; charset=utf-8")[0] == 401


def test_invalid_json_400_and_deep_nesting(started):
    _, srv = started()
    st, _, body = req(srv.port, body=b"{not json")
    assert st == 400 and b"not json" not in body
    st, _, _ = req(srv.port, body=b"[" * 50000 + b"]" * 50000)
    assert st in (400, 413)


def test_enroll_code_replay_401(started):
    hub, srv = started()
    creds, code = enrol(hub, srv.port)
    st, _, body = jpost(srv.port, "/assistant/enroll", {"code": code, "device_id": "dev1"})
    assert st == 401 and json.loads(body) == {"error": "unauthorized"}


def test_enroll_failures_are_byte_identical(started):
    hub, srv = started()
    _, code = hub.registry.create_enrollment(tenant_id="default", user_id="u1", name="box", harness="pi")
    attempts = [
        {"code": "hub_enr_nope", "device_id": "d"},
        {"code": code},                        # missing device
        {"code": code, "device_id": ""},       # empty device
        {"code": 5, "device_id": "d"},
        {"code": "x" * 5000, "device_id": "d"},
        ["not", "an", "object"],
        {"code": code, "device_id": "d" * 5000},
    ]
    seen = set()
    for a in attempts:
        st, headers, body = jpost(srv.port, "/assistant/enroll", a)
        seen.add((st, body, headers["content-length"]))
    st, _, body = req(srv.port, path="/assistant/enroll", body=b"{garbage")
    seen.add((st, body, str(len(body))))
    assert len(seen) == 1 and next(iter(seen))[0] == 401
    # the valid code was never consumed by those failures
    st, _, _ = jpost(srv.port, "/assistant/enroll", {"code": code, "device_id": "d"})
    assert st == 200


def test_hook_events_recorded_attested_and_mcp_report_is_not(started):
    hub, srv = started()
    creds, _ = enrol(hub, srv.port)
    auth = {"Authorization": f"Worker {creds['credential']}", "X-Herdr-Device": "dev1"}
    st, _, body = jpost(srv.port, "/assistant/hook-events",
                        {"session_id": "s1", "kind": "observation", "payload": {"tool": "Bash"}}, headers=auth)
    assert st == 200 and json.loads(body)["event_id"].startswith("evt_")
    st, _, _ = mcp(srv.port, creds["credential"], "dev1", "tools/call",
                   {"name": "report", "arguments": {"session_id": "s1", "kind": "decision", "payload": {"a": 1}}})
    assert st == 200
    events = list_events(hub.store, tenant_id="default", user_id="u1")
    by_kind = {e.kind: e.provenance for e in events}
    assert by_kind["observation"] == "harness_attested"
    assert by_kind["report"] == "worker_reported"


def test_hook_events_validation_auth_scope_and_gate(started):
    hub, srv = started()
    creds, _ = enrol(hub, srv.port)
    auth = {"Authorization": f"Worker {creds['credential']}", "X-Herdr-Device": "dev1"}
    for bad in ({"session_id": "s", "kind": "decision", "payload": {}},
                {"session_id": "", "kind": "observation", "payload": {}},
                {"session_id": "s", "kind": "observation", "payload": "str"},
                [1]):
        st, _, body = jpost(srv.port, "/assistant/hook-events", bad, headers=auth)
        assert st == 400 and json.loads(body) == {"error": "invalid event"}
    st, _, b1 = jpost(srv.port, "/assistant/hook-events", {}, headers={**auth, "X-Herdr-Device": "zzz"})
    st2, _, b2 = jpost(srv.port, "/assistant/hook-events", {}, headers={})
    assert (st, st2) == (401, 401) and b1 == b2
    hub.store.workers[creds["worker_id"]].scopes = ["ask"]
    st, _, _ = jpost(srv.port, "/assistant/hook-events",
                     {"session_id": "s", "kind": "observation", "payload": {}}, headers=auth)
    assert st == 403
    hub2, srv2 = started(make_hub(enabled=False))
    creds2, _ = enrol(hub2, srv2.port)
    auth2 = {"Authorization": f"Worker {creds2['credential']}", "X-Herdr-Device": "dev1"}
    good = {"session_id": "s", "kind": "observation", "payload": {}}
    # unauthenticated peers get 401 even when the assistant is disabled: enablement is never revealed to them
    for headers in ({}, {**auth2, "X-Herdr-Device": "zzz"}, {"Authorization": "Worker hcw_nope", "X-Herdr-Device": "d"}):
        assert jpost(srv2.port, "/assistant/hook-events", good, headers=headers)[0] == 401
    assert jpost(srv2.port, "/assistant/hook-events", good, headers=auth2)[0] == 503


def test_hook_events_per_worker_rate_limit_429(started):
    hub, srv = started()
    creds, _ = enrol(hub, srv.port)
    auth = {"Authorization": f"Worker {creds['credential']}", "X-Herdr-Device": "dev1"}
    good = {"session_id": "s", "kind": "observation", "payload": {}}
    statuses = [jpost(srv.port, "/assistant/hook-events", good, headers=auth)[0] for _ in range(32)]
    assert statuses == [200] * 30 + [429] * 2
    assert len(list_events(hub.store, tenant_id="default", user_id="u1", limit=500)) == 30


def test_rate_limit_429_before_auth_work(started):
    hub, srv = started(rate_limit=5, enroll_rate_limit=2)
    creds, _ = enrol(hub, srv.port)           # 1 enroll request
    before = len(hub.store.audit)
    wrong = {"Authorization": f"Worker {creds['credential']}", "X-Herdr-Device": "wrong"}
    statuses = [jpost(srv.port, "/assistant/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                      headers=wrong)[0] for _ in range(12)]
    assert statuses[:5] == [401] * 5 and statuses[5:] == [429] * 7
    assert len(hub.store.audit) == before + 1          # failed-auth audit is coalesced per worker and reason
    # enroll has its own, smaller bucket
    codes = [jpost(srv.port, "/assistant/enroll", {"code": "x", "device_id": "d"})[0] for _ in range(4)]
    assert codes == [401, 429, 429, 429]


def test_unknown_path_404_and_get_405(started):
    _, srv = started()
    assert req(srv.port, path="/nope", body=b"{}")[0] == 404
    assert req(srv.port, path="/assistant/mcp/extra", body=b"{}")[0] == 404
    for path in ("/assistant/mcp", "/assistant/enroll", "/assistant/hook-events"):
        for method in ("GET", "PUT", "DELETE", "HEAD", "OPTIONS"):
            st, headers, _ = req(srv.port, method=method, path=path, ctype=None, length=False)
            assert st == 405, (method, path)


def test_unexpected_exception_is_500_without_detail(started):
    hub, srv = started()
    creds, _ = enrol(hub, srv.port)
    hub.store.worker_events = None                       # real storage corruption: record_event raises TypeError
    auth = {"Authorization": f"Worker {creds['credential']}", "X-Herdr-Device": "dev1"}
    st, _, body = jpost(srv.port, "/assistant/hook-events",
                        {"session_id": "s", "kind": "observation", "payload": {}}, headers=auth)
    assert st == 500 and json.loads(body) == {"error": "internal"}
    assert b"NoneType" not in body and b"Error" not in body


def test_port_zero_assigns_port_and_shutdown_is_idempotent():
    srv = HubServer(make_hub(), host="127.0.0.1", port=0, allowed_networks=LOOP, allowed_hosts=[])
    assert srv.port > 0
    srv.start()
    assert srv.wait_until_ready()
    srv.shutdown()
    srv.shutdown()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _service(pa):
    return AssistantService(config=assistant_config({"personal_assistant": pa}))


def test_cli_starts_hub_when_enabled():
    """Exercises the extracted serve-wiring function with a real config (the full `control-plane serve` command
    needs a fleet repo, Postgres-less but heavyweight; the wiring it calls is this function)."""
    from herdr_engineering.cli import _start_assistant_hub

    port = _free_port()
    svc = _service({"enabled": True, "hub": {"enabled": True, "host": "127.0.0.1", "port": port,
                                             "allowed_networks": ["127.0.0.0/8"]}})
    server, registry = _start_assistant_hub(svc)
    try:
        assert server is not None and server.port == port and server.wait_until_ready()
        _, code = registry.create_enrollment(tenant_id="default", user_id="u1", name="box", harness="pi")
        st, _, body = jpost(port, "/assistant/enroll", {"code": code, "device_id": "d1"})
        assert st == 200
        cred = json.loads(body)["credential"]
        st, _, body = mcp(port, cred, "d1", "initialize")
        assert st == 200 and json.loads(body)["result"]["serverInfo"]["name"] == "herdr-assistant"
    finally:
        server.shutdown()


def test_cli_does_not_start_hub_when_disabled():
    from herdr_engineering.cli import _start_assistant_hub

    assert _start_assistant_hub(_service({"enabled": True})) == (None, None)
    assert _start_assistant_hub(_service({"enabled": False, "hub": {"enabled": True}})) == (None, None)


def test_cli_refuses_wildcard_hub_bind():
    from herdr_engineering.cli import _start_assistant_hub

    svc = _service({"enabled": True, "hub": {"enabled": True, "host": "0.0.0.0"}})
    with pytest.raises(SystemExit, match="assistant hub cannot start"):
        _start_assistant_hub(svc)


# ---- fix round 1 -----------------------------------------------------------------------------------------------

def _wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_slowloris_trickle_closed_at_deadline_and_slot_released(started):
    hub, srv = started(request_deadline=0.5, max_connections_per_peer=2)
    for _ in range(3):                      # 3 > cap of 2: a leaked slot would starve the normal request below
        s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        t0 = time.monotonic()
        closed = False
        s.sendall(b"POST /assistant/mcp HTTP/1.1\r\n")
        while time.monotonic() - t0 < 4:
            try:
                s.sendall(b"X")             # one byte every 0.2 s keeps every per-recv timeout alive
                s.settimeout(0.2)
                if s.recv(1) == b"":
                    closed = True
                    break
            except TimeoutError:
                continue
            except OSError:
                closed = True
                break
        elapsed = time.monotonic() - t0
        s.close()
        assert closed and elapsed < 2.0, elapsed
    creds, _ = enrol(hub, srv.port)
    assert mcp(srv.port, creds["credential"], "dev1", "tools/list")[0] == 200


def test_slow_body_also_hits_deadline(started):
    _, srv = started(request_deadline=0.5, max_connections_per_peer=2)
    with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as s:
        s.sendall(b"POST /assistant/mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                  b"Content-Length: 50\r\n\r\n{")
        t0 = time.monotonic()
        s.settimeout(3)
        data = s.recv(100)
        assert time.monotonic() - t0 < 2.0
        assert b"200" not in data[:15]


def test_per_peer_connection_cap(started):
    hub, srv = started(max_connections_per_peer=3)
    idle = [socket.create_connection(("127.0.0.1", srv.port), timeout=5) for _ in range(3)]
    try:
        assert _wait_for(lambda: srv._httpd.peer_connections("127.0.0.1") == 3)
        st, _, _ = raw(srv.port, b"")        # cap+1: refused at accept with 503 and no request parsing
        assert st == 503
    finally:
        for c in idle:
            c.close()
    assert _wait_for(lambda: srv._httpd.peer_connections("127.0.0.1") == 0)    # counter never leaks
    creds, _ = enrol(hub, srv.port)
    assert mcp(srv.port, creds["credential"], "dev1", "tools/list")[0] == 200


def test_rate_limiter_is_bounded():
    from herdr_engineering.control_plane.assistant_hub.server import _RateLimiter

    lim = _RateLimiter(max_keys=3)
    for i in range(50):
        assert lim.allow((f"10.0.0.{i}", "g"), 5)
        assert len(lim._hits) <= 3
    # a busy key is not evicted while it is the most recently used
    for _ in range(5):
        assert lim.allow(("hot", "g"), 5)
    assert not lim.allow(("hot", "g"), 5)


def test_server_passes_tracking_cap():
    srv = HubServer(make_hub(), host="127.0.0.1", port=0, allowed_networks=LOOP, allowed_hosts=[],
                    max_tracked_peers=3)
    try:
        assert srv._limiter._max == 3
    finally:
        srv.shutdown()


@pytest.mark.parametrize("line", [b"Transfer-Encoding : chunked", b"Content-Length : 2", b"Content-Length\t: 2",
                                  b"X-Folded: a\r\n b", b"X-Folded: a\r\n\tb", b"NoColonHere"])
def test_header_whitespace_and_obs_fold_rejected(started, line):
    _, srv = started()
    head = (b"POST /assistant/mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            + line + b"\r\n")
    if b"Content-Length" not in line:
        head += b"Content-Length: 2\r\n"
    st, _, _ = raw(srv.port, head + b"\r\n{}")
    assert st == 400


def test_peer_networks_must_be_subset_of_allowed():
    hub = make_hub()
    with pytest.raises(ValueError):
        HubServer(hub, host="127.0.0.1", port=0, allowed_networks=LOOP, allowed_hosts=[],
                  peer_networks=["100.64.0.0/10"])
    with pytest.raises(ValueError):
        HubServer(hub, host="127.0.0.1", port=0, allowed_networks=LOOP, allowed_hosts=[],
                  peer_networks=["127.0.0.0/8", "0.0.0.0/0"])
    HubServer(hub, host="127.0.0.1", port=0, allowed_networks=LOOP, allowed_hosts=[],
              peer_networks=["127.0.0.2/32"]).shutdown()


def test_ipv6_loopback_bind_serves():
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
    except OSError:
        pytest.skip("host has no IPv6 loopback (::1 cannot be bound)")
    hub = make_hub()
    srv = HubServer(hub, host="::1", port=0, allowed_networks=["::1/128"], allowed_hosts=[])
    srv.start()
    assert srv.wait_until_ready()
    try:
        _, code = hub.registry.create_enrollment(tenant_id="default", user_id="u1", name="b", harness="pi")
        body = json.dumps({"code": code, "device_id": "d"}).encode()
        head = (f"POST /assistant/enroll HTTP/1.1\r\nHost: [::1]:{srv.port}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n").encode()
        with socket.create_connection(("::1", srv.port), timeout=5) as s:
            s.sendall(head + body)
            assert s.recv(65536).startswith(b"HTTP/1.1 200")
    finally:
        srv.shutdown()


def test_mapped_ipv4_peer_classified_by_ipv4_form():
    srv = HubServer(make_hub(), host="127.0.0.1", port=0, allowed_networks=LOOP + ["100.64.0.0/10"],
                    allowed_hosts=[])
    try:
        assert srv.peer_permitted("::ffff:100.64.1.2") is True
        assert srv.peer_permitted("::ffff:8.8.8.8") is False
        assert srv.peer_permitted("100.64.1.2") is True
        assert srv.peer_permitted("8.8.8.8") is False
        assert srv.peer_permitted("fe80::1%eth0") is False
    finally:
        srv.shutdown()
