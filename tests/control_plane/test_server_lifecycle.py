"""Real HTTP server shutdown must release its periodic maintenance workers."""
import errno
import socket
import threading
import time
import urllib.request

import pytest

from herdr_engineering.control_plane import api
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.repo import ControlPlaneRepo


@pytest.mark.parametrize("with_collab", [False, True])
def test_repeated_server_shutdown_stops_maintenance_and_releases_listeners(with_collab):
    workers = set()
    for _ in range(3):
        repo = ControlPlaneRepo()
        collab = CollabService(StaticTokenAuthenticator({})) if with_collab else None
        server = ControlPlaneServer(repo, collab=collab, public_port=0 if collab else None)
        swept, maintained = threading.Event(), threading.Event()
        sweep = server.sweep_once

        def sweep_once():
            workers.add(threading.current_thread())
            sweep()
            swept.set()

        server.sweep_once = sweep_once
        if collab:
            maintain = collab.maintain

            def maintain_once():
                workers.add(threading.current_thread())
                maintain()
                maintained.set()

            collab.maintain = maintain_once
        serving = threading.Thread(target=server.serve, daemon=True)
        serving.start()
        try:
            server.wait_until_ready()
            assert swept.wait(5)
            if collab:
                assert maintained.wait(5)
            with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/v1/health", timeout=5) as response:
                assert response.status == 200
                response.read()
            listeners = [listener for listener in (server._httpd, server._public_httpd) if listener]
            started = time.monotonic()
            server.shutdown()
            assert time.monotonic() - started < 5, "shutdown waited for a periodic maintenance interval"
            serving.join(timeout=1)
            assert not serving.is_alive()
            assert all(not worker.is_alive() for worker in workers), "closed servers retain maintenance workers"
            assert all(listener.socket.fileno() == -1 for listener in listeners)
            assert server._httpd is None and server._public_httpd is None
            server.shutdown()  # Closing an already closed server must be harmless.
        finally:
            server.shutdown()
            serving.join(timeout=5)


def test_shutdown_waits_for_inflight_maintenance_then_prevents_more_work():
    server = ControlPlaneServer(ControlPlaneRepo())
    entered, release, finished, stopped = (threading.Event() for _ in range(4))
    workers = []
    sweep = server.sweep_once

    def sweep_once():
        workers.append(threading.current_thread())
        entered.set()
        release.wait(5)
        sweep()
        finished.set()

    def shutdown():
        server.shutdown()
        stopped.set()

    server.sweep_once = sweep_once
    serving = threading.Thread(target=server.serve, daemon=True)
    stopping = threading.Thread(target=shutdown, daemon=True)
    serving.start()
    try:
        server.wait_until_ready()
        assert entered.wait(5)
        stopping.start()
        assert not stopped.wait(1), "shutdown returned while maintenance could still mutate the repository"
        release.set()
        assert stopped.wait(5)
        assert finished.is_set()
        assert len(workers) == 1 and not workers[0].is_alive()
    finally:
        release.set()
        if stopping.ident is not None:
            stopping.join(timeout=5)
        server.shutdown()
        serving.join(timeout=5)


@pytest.mark.parametrize("collab_only", [False, True])
def test_shutdown_during_secondary_listener_construction_closes_every_listener(monkeypatch, collab_only):
    collab = CollabService(StaticTokenAuthenticator({}))
    options = dict(operator_port=0, operator_user="owner", operator_hosts=["localhost"]) if collab_only else {}
    server = ControlPlaneServer(ControlPlaneRepo(), collab=collab, public_port=0,
                                collab_only=collab_only, **options)
    constructing, release, shutdown_primary, secondary_serving, stopped = (
        threading.Event() for _ in range(5))
    listeners, errors = [], []
    listener_type = api._DaemonThreadingServer

    def make_listener(*args, **kwargs):
        listener = listener_type(*args, **kwargs)
        listeners.append(listener)
        if len(listeners) == 1:
            shutdown = listener.shutdown

            def shutdown_first():
                shutdown_primary.set()
                shutdown()

            listener.shutdown = shutdown_first
        else:
            serve_forever = listener.serve_forever

            def serve_secondary():
                secondary_serving.set()
                serve_forever()

            listener.serve_forever = serve_secondary
            constructing.set()
            assert release.wait(10), "test did not release listener construction"
        return listener

    def serve():
        try:
            server.serve()
        except Exception as error:
            errors.append(error)

    def shutdown():
        try:
            server.shutdown()
        except Exception as error:
            errors.append(error)
        finally:
            stopped.set()

    monkeypatch.setattr(api, "_DaemonThreadingServer", make_listener)
    serving = threading.Thread(target=serve, daemon=True)
    stopping = threading.Thread(target=shutdown, daemon=True)
    serving.start()
    try:
        assert constructing.wait(5)
        stopping.start()
        assert server._maintenance_stop.wait(5)
        # In the broken implementation this proves shutdown has already skipped
        # the not-yet-published secondary listener. With synchronization it waits.
        shutdown_primary.wait(1)
        release.set()
        assert stopped.wait(5), "shutdown missed the listener constructed concurrently"
        serving.join(timeout=5)
        assert not serving.is_alive()
        assert not errors
        assert all(listener.socket.fileno() == -1 for listener in listeners)
        assert not server._background_threads
        assert server._httpd is server._public_httpd is server._operator_httpd is None
        assert not server._ready.is_set()
    finally:
        release.set()
        # Also clean up the leaked real listener when testing the broken code.
        if stopping.is_alive() and secondary_serving.wait(5):
            listeners[1].shutdown()
        if stopping.ident is not None:
            stopping.join(timeout=5)
        serving.join(timeout=5)
        for listener in listeners:
            listener.server_close()


def test_shutdown_before_serve_does_not_start_listeners_or_maintenance():
    server = ControlPlaneServer(ControlPlaneRepo())
    server.shutdown()
    serving = threading.Thread(target=server.serve, daemon=True)
    serving.start()
    try:
        serving.join(timeout=1)
        assert not serving.is_alive()
        assert server._httpd is None
        assert not server._ready.is_set()
        assert not server._background_threads
    finally:
        server.shutdown()
        serving.join(timeout=5)


@pytest.mark.parametrize(("collab_only", "failure"), [
    (False, "bind"), (True, "bind"),
    (False, "secondary_thread"), (True, "secondary_thread"),
    (False, "maintenance_thread"),
])
def test_failed_startup_closes_started_and_unstarted_listeners(monkeypatch, collab_only, failure):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        secondary_port = occupied.getsockname()[1] if failure == "bind" else 0
        options = dict(operator_port=secondary_port, operator_user="owner", operator_hosts=["localhost"]) if collab_only else {}
        server = ControlPlaneServer(
            ControlPlaneRepo(), collab=CollabService(StaticTokenAuthenticator({})),
            public_port=0 if collab_only else secondary_port, collab_only=collab_only, **options)
        listeners, workers, startup_errors, shutdown_errors = [], [], [], []
        listener_type, start_thread = api._DaemonThreadingServer, threading.Thread.start

        def make_listener(*args, **kwargs):
            listener = listener_type(*args, **kwargs)
            entered = threading.Event()
            serve_forever = listener.serve_forever

            def serve_listener():
                entered.set()
                serve_forever()

            listener.serve_forever = serve_listener
            listeners.append((listener, entered))
            return listener

        def start(thread):
            if (failure == "secondary_thread" and len(listeners) == 2
                    and thread._target == listeners[1][0].serve_forever):
                raise RuntimeError("secondary thread startup failed")
            if failure == "maintenance_thread" and thread._target == server._sweep_loop:
                raise RuntimeError("maintenance thread startup failed")
            start_thread(thread)
            workers.append(thread)

        def serve():
            try:
                server.serve()
            except Exception as error:
                startup_errors.append(error)

        def shutdown():
            try:
                server.shutdown()
            except Exception as error:
                shutdown_errors.append(error)

        monkeypatch.setattr(api, "_DaemonThreadingServer", make_listener)
        monkeypatch.setattr(threading.Thread, "start", start)
        serving = threading.Thread(target=serve, daemon=True)
        stopping = threading.Thread(target=shutdown, daemon=True)
        serving.start()
        try:
            serving.join(timeout=5)
            assert not serving.is_alive(), "failed startup did not return"
            assert len(startup_errors) == 1
            error = startup_errors[0]
            if failure == "bind":
                assert isinstance(error, OSError) and error.errno == errno.EADDRINUSE
            else:
                assert isinstance(error, RuntimeError) and "thread startup failed" in str(error)
            stopping.start()
            stopping.join(timeout=3)
            assert not stopping.is_alive(), "shutdown waited on a listener that never started"
            assert not shutdown_errors
            assert all(listener.socket.fileno() == -1 for listener, _ in listeners)
            assert all(not worker.is_alive() for worker in workers)
            assert server._httpd is server._public_httpd is server._operator_httpd is None
            assert not server._ready.is_set()
            assert not server._background_threads
            server.shutdown()
        finally:
            monkeypatch.setattr(threading.Thread, "start", start_thread)
            # Unstick the real stdlib shutdown on the broken implementation so
            # the regression fails within its deadline without leaking threads.
            rescuers = []
            for listener, entered in listeners:
                if listener.socket.fileno() != -1 and not entered.is_set():
                    rescuer = threading.Thread(target=listener.serve_forever, daemon=True)
                    rescuer.start()
                    rescuers.append(rescuer)
            if stopping.ident is not None:
                stopping.join(timeout=5)
            for listener, entered in listeners:
                if listener.socket.fileno() != -1 and entered.is_set():
                    listener.shutdown()
                listener.server_close()
            serving.join(timeout=5)
            for rescuer in rescuers:
                rescuer.join(timeout=5)
