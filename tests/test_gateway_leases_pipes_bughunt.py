"""Regression tests for the gateway lease/pipe bug-hunt findings (real sockets, real registry, no mocks)."""
import select
import socket
import threading
import time

import pytest

from herdr_engineering.devfabric import (
    DevServiceRegistry,
    _Activity,
    _add_seconds,
    _now_iso,
    _pump,
    prefixed_pipe,
    proxy_pair,
    rewriting_pipe,
)
from herdr_engineering.fabric import FabricGateway, GatewayRouter
from herdr_engineering.fabric_urls import UnsafeTarget

_RNG = (26000, 26999)


def _reg(tmp_path, ttl=90):
    return DevServiceRegistry(store_path=tmp_path / "leases.json", port_range=_RNG,
                              lease_ttl_seconds=ttl)


def _gateway(tmp_path):
    return FabricGateway(_reg(tmp_path), GatewayRouter(["127.0.0.1"]), control_host="127.0.0.1",
                         control_port=0, url_base="dev.test", probe_interval=30, token=None)


# -- finding 4: route hosts are validated like the target host --------------------------------

_BODY = {"target_machine_id": "m", "target_host": "127.0.0.1", "target_port": 9}


@pytest.mark.parametrize("host", ["169.254.169.254", "10.1.2.3", "internal.example"])
def test_register_refuses_unsafe_route_host(tmp_path, host):
    gw = _gateway(tmp_path)
    with pytest.raises(UnsafeTarget):
        gw.op_register(dict(_BODY, routes=[{"prefix": "/", "host": host, "port": 80}]))
    assert gw.registry.active_leases() == []


def test_register_refuses_unsafe_route_host_on_existing_lease_id(tmp_path):
    gw = _gateway(tmp_path)
    ok = gw.op_register(dict(_BODY, lease_id="lease_fixed"))["lease"]
    with pytest.raises(UnsafeTarget):
        gw.op_register(dict(_BODY, lease_id=ok["id"],
                            routes=[{"prefix": "/a", "host": "169.254.169.254", "port": 80}]))
    assert not gw.registry.get(ok["id"]).routes
    gw.stop()


def test_register_accepts_loopback_route_host(tmp_path):
    gw = _gateway(tmp_path)
    lease = gw.op_register(dict(_BODY, routes=[{"prefix": "/api", "host": "127.0.0.1", "port": 8080}]))["lease"]
    assert lease["routes"]
    gw.stop()


# -- finding 16: heartbeat must not revive a lease onto a re-allocated port -------------------

def test_heartbeat_does_not_revive_lease_whose_port_was_reallocated(tmp_path):
    reg = _reg(tmp_path)
    a = reg.register(target_machine_id="x", target_host="127.0.0.1", target_port=9001, preferred_port=26500)
    assert a.external_port == 26500
    reg.close(a.id)
    b = reg.register(target_machine_id="y", target_host="127.0.0.1", target_port=9002, preferred_port=26500)
    assert b.external_port == 26500
    assert reg.heartbeat(a.id) is None  # agent re-registers instead
    owners = [lease.id for lease in reg.active_leases() if lease.external_port == 26500]
    assert owners == [b.id]


def test_heartbeat_still_revives_expired_lease_when_port_is_free(tmp_path):
    reg = _reg(tmp_path)
    a = reg.register(target_machine_id="x", target_host="127.0.0.1", target_port=9001)
    reg.close(a.id)
    assert reg.heartbeat(a.id).state == "active"


# -- finding 19: idempotent re-register refreshes the lapsed lease -----------------------------

def _lapse(reg, lease):
    lease.state = "expired"
    lease.expires_at = _add_seconds(_now_iso(), -600)
    reg._persist()


@pytest.mark.parametrize("by", ["owner", "lease_id"])
def test_reregister_lapsed_lease_refreshes_expiry(tmp_path, by):
    reg = _reg(tmp_path)
    kw = dict(target_machine_id="m", target_host="127.0.0.1", target_port=9100,
              owner_herdr_session_id="s", owner_process_id="p")
    a = reg.register(**kw)
    _lapse(reg, a)
    again = reg.register(**(kw if by == "owner" else dict(target_machine_id="m", target_host="127.0.0.1",
                                                          target_port=9100, lease_id=a.id)))
    assert again.id == a.id and again.state == "active"
    assert reg.reconcile() == []  # not expired again at the next reconcile
    assert again.state == "active"


def test_reregister_lapsed_lease_moves_off_a_port_taken_meanwhile(tmp_path):
    reg = _reg(tmp_path)
    kw = dict(target_machine_id="m", target_host="127.0.0.1", target_port=9101,
              owner_herdr_session_id="s", owner_process_id="p", preferred_port=26600)
    a = reg.register(**kw)
    assert a.external_port == 26600
    reg.close(a.id)
    b = reg.register(target_machine_id="o", target_host="127.0.0.1", target_port=9102, preferred_port=26600)
    again = reg.register(**kw)
    assert again.id == a.id and again.external_port != b.external_port
    ports = [lease.external_port for lease in reg.active_leases()]
    assert len(ports) == len(set(ports))


def test_register_is_safe_against_concurrent_registration(tmp_path):
    reg = _reg(tmp_path)
    kw = dict(target_machine_id="m", target_host="127.0.0.1", target_port=9200,
              owner_herdr_session_id="s", owner_process_id="p")
    reg.register(**kw)
    errors: list[BaseException] = []
    stop = threading.Event()

    def churn() -> None:
        i = 0
        while not stop.is_set():
            try:
                reg.register(target_machine_id="c", target_host="127.0.0.1", target_port=9300 + i % 50)
            except RuntimeError as exc:
                errors.append(exc)
            except Exception:  # ConflictError once the range fills is not what we test
                pass
            i += 1

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    try:
        for _ in range(300):
            try:
                reg.register(**kw)
            except RuntimeError as exc:
                errors.append(exc)
    finally:
        stop.set()
        t.join(5)
    assert not [e for e in errors if isinstance(e, RuntimeError)]


# -- finding 18: one-way streams are not cut by a per-direction timeout ------------------------

def _stream_through(pipe_fn, idle):
    """Backend streams a byte every 0.15 s for ~3 s; the client stays silent. Returns bytes received."""
    c_user, c_proxy = socket.socketpair()
    t_proxy, t_backend = socket.socketpair()
    pt = threading.Thread(target=pipe_fn, args=(c_proxy, t_proxy, idle), daemon=True)
    pt.start()
    sent = 20

    def backend() -> None:
        for _ in range(sent):
            # Like Node's http server: a half-close from the proxy ends the response.
            if select.select([t_backend], [], [], 0)[0] and t_backend.recv(1) == b"":
                break
            t_backend.sendall(b"x")
            time.sleep(0.15)
        t_backend.shutdown(socket.SHUT_WR)

    threading.Thread(target=backend, daemon=True).start()
    got = b""
    c_user.settimeout(10)
    try:
        while True:
            d = c_user.recv(100)
            if not d:
                break
            got += d
    except OSError:
        pass
    pt.join(15)
    for s in (c_user, t_backend):
        s.close()
    return got, sent


def test_proxy_pair_keeps_streaming_when_only_one_direction_is_active():
    got, sent = _stream_through(lambda c, t, idle: proxy_pair(c, t, inactivity_timeout=idle), 0.5)
    assert len(got) == sent


def test_prefixed_pipe_keeps_streaming_when_only_one_direction_is_active():
    got, sent = _stream_through(lambda c, t, idle: prefixed_pipe(c, t, inactivity_timeout=idle), 0.5)
    assert len(got) == sent


def test_proxy_pair_still_ends_when_both_directions_are_idle():
    c_user, c_proxy = socket.socketpair()
    t_proxy, t_backend = socket.socketpair()
    started = time.monotonic()
    proxy_pair(c_proxy, t_proxy, inactivity_timeout=0.5)
    assert time.monotonic() - started < 5
    c_user.close()
    t_backend.close()


# -- finding 5: rewriting_pipe is bounded and keeps connections alive --------------------------

def test_rewriting_pipe_returns_when_client_never_sends_a_head():
    c_user, c_proxy = socket.socketpair()
    t_proxy, t_backend = socket.socketpair()
    started = time.monotonic()
    rewriting_pipe(c_proxy, t_proxy, 80, "dev.test:80", inactivity_timeout=0.5)
    assert time.monotonic() - started < 5
    c_user.close()
    t_backend.close()


def test_rewriting_pipe_returns_when_origin_goes_silent_after_the_request():
    c_user, c_proxy = socket.socketpair()
    t_proxy, t_backend = socket.socketpair()
    c_user.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    started = time.monotonic()
    rewriting_pipe(c_proxy, t_proxy, 80, "dev.test:80", inactivity_timeout=0.5, response_timeout=0.5)
    assert time.monotonic() - started < 5
    c_user.close()
    t_backend.close()


def test_rewriting_pipe_waits_past_the_inactivity_timeout_for_a_slow_origin_head():
    """A slow origin (first byte after > inactivity_timeout) is still served."""
    c_user, c_proxy = socket.socketpair()
    t_proxy, t_backend = socket.socketpair()
    c_user.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")

    def origin() -> None:
        t_backend.recv(65536)
        time.sleep(1.2)
        t_backend.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        t_backend.shutdown(socket.SHUT_WR)

    threading.Thread(target=origin, daemon=True).start()
    pt = threading.Thread(target=rewriting_pipe, args=(c_proxy, t_proxy, 80, "dev.test:80"),
                          kwargs={"inactivity_timeout": 0.4, "response_timeout": 5.0}, daemon=True)
    pt.start()
    c_user.settimeout(10)
    got = b""
    while chunk := c_user.recv(4096):
        got += chunk
    pt.join(10)
    assert got.startswith(b"HTTP/1.1 200") and got.endswith(b"ok")
    c_user.close()
    t_backend.close()


def test_rewriting_pipe_answers_504_when_origin_never_sends_a_head():
    c_user, c_proxy = socket.socketpair()
    t_proxy, t_backend = socket.socketpair()
    c_user.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    rewriting_pipe(c_proxy, t_proxy, 80, "dev.test:80", inactivity_timeout=0.5, response_timeout=0.5)
    c_user.settimeout(5)
    assert c_user.recv(4096).startswith(b"HTTP/1.1 504")
    c_user.close()
    t_backend.close()


def test_rewriting_pipe_streams_a_slow_response_past_the_idle_timeout():
    c_user, c_proxy = socket.socketpair()
    t_proxy, t_backend = socket.socketpair()
    c_user.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    pt = threading.Thread(target=rewriting_pipe, args=(c_proxy, t_proxy, 80, "dev.test:80"),
                          kwargs={"inactivity_timeout": 0.5}, daemon=True)
    pt.start()

    def origin() -> None:
        t_backend.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        for _ in range(15):
            t_backend.sendall(b"1\r\nx\r\n")
            time.sleep(0.15)
        t_backend.sendall(b"0\r\n\r\n")
        t_backend.shutdown(socket.SHUT_WR)

    threading.Thread(target=origin, daemon=True).start()
    got = b""
    c_user.settimeout(10)
    while True:
        d = c_user.recv(4096)
        if not d:
            break
        got += d
    pt.join(10)
    assert got.count(b"1\r\nx\r\n") == 15
    c_user.close()
    t_backend.close()


def test_gateway_enables_tcp_keepalive_on_accepted_clients(tmp_path):
    from herdr_engineering.devfabric import enable_keepalive
    s = socket.socket()
    try:
        enable_keepalive(s)
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
    finally:
        s.close()


# -- review: an early origin answer must not truncate on a failed upload ------------------------

def test_rewriting_pipe_delivers_the_whole_early_response_when_the_upload_is_cut():
    c_user, c_proxy = socket.socketpair()
    t_proxy, t_backend = socket.socketpair()
    for s in (c_user, c_proxy, t_proxy, t_backend):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    body = b"r" * 150000
    c_user.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 10000000\r\n\r\n")

    def uploader() -> None:
        try:
            while True:
                c_user.sendall(b"u" * 8192)
        except OSError:
            pass

    def origin() -> None:
        t_backend.recv(65536)
        t_backend.sendall(b"HTTP/1.1 413 Too Large\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        t_backend.close()  # origin answers early, then closes: further upload sends fail

    threading.Thread(target=uploader, daemon=True).start()
    threading.Thread(target=origin, daemon=True).start()
    pt = threading.Thread(target=rewriting_pipe, args=(c_proxy, t_proxy, 80, "dev.test:80"),
                          kwargs={"inactivity_timeout": 3.0}, daemon=True)
    pt.start()
    got = b""
    c_user.settimeout(10)
    time.sleep(0.5)  # the client reads late: the response pump is blocked mid-body when the upload send fails
    while True:
        try:
            d = c_user.recv(65536)
        except OSError:
            break
        if not d:
            break
        got += d
        if len(got.split(b"\r\n\r\n", 1)[-1]) >= len(body):
            break
    pt.join(10)
    assert len(got.split(b"\r\n\r\n", 1)[-1]) == len(body)
    c_user.close()


def test_pump_send_failure_does_not_stop_the_opposite_direction():
    a, b = socket.socketpair()
    dead_dst, dead_peer = socket.socketpair()
    dead_peer.close()
    act = _Activity()
    a.settimeout(0.1)
    b.sendall(b"x")
    _pump(a, dead_dst, act, 5.0)
    assert not act.stop.is_set()
    a.close()
    b.close()
    dead_dst.close()


# -- review: local (gateway-less) callers re-register a lease that lapsed onto a taken port ---

def test_local_heartbeat_reregisters_lease_whose_port_was_reallocated(tmp_path):
    from herdr_engineering.cli import heartbeat_local_leases
    reg = _reg(tmp_path)
    a = reg.register(target_machine_id="x", target_host="127.0.0.1", target_port=9001, preferred_port=26500)
    reg.close(a.id)
    b = reg.register(target_machine_id="y", target_host="127.0.0.1", target_port=9002, preferred_port=26500)
    heartbeat_local_leases(reg, {"k": {"lease_id": a.id}})
    again = reg.get(a.id)
    assert again.state == "active"
    assert again.external_port != b.external_port
    assert {lease.id for lease in reg.active_leases()} == {a.id, b.id}


# -- review: resolve_port/get hold the registry lock --------------------------------------------

def test_registry_get_and_resolve_port_hold_the_lock(tmp_path):
    reg = _reg(tmp_path)
    lease = reg.register(target_machine_id="x", target_host="127.0.0.1", target_port=9001)
    results = []
    with reg._lock:
        t = threading.Thread(target=lambda: results.append(reg.get(lease.id)), daemon=True)
        t.start()
        t.join(0.3)
        blocked = t.is_alive()
    t.join(5)
    assert blocked and results == [lease]


def test_reregister_lapsed_lease_honours_preferred_port_when_its_port_was_taken(tmp_path):
    reg = _reg(tmp_path)
    a = reg.register(target_machine_id="x", target_host="127.0.0.1", target_port=9001, preferred_port=26500)
    reg.close(a.id)
    reg.register(target_machine_id="y", target_host="127.0.0.1", target_port=9002, preferred_port=26500)
    again = reg.register(target_machine_id="x", target_host="127.0.0.1", target_port=9001,
                         lease_id=a.id, preferred_port=26777)
    assert again.external_port == 26777
