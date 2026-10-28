from herdr_engineering.control_plane.request_metrics import RequestMetrics


def test_metrics_bound_route_cardinality_and_preserve_failure_causes():
    metrics = RequestMetrics()
    for i in range(400):
        metrics.record(f'/api/v1/sessions/private-{i}?token=secret', 25, 200)
    metrics.record('/api/v1/sessions/x', 2001, 503, 'TimeoutError')
    snapshot = metrics.snapshot()
    assert len(snapshot['by_route']) == 1
    route = snapshot['by_route']['/api/v1/sessions/:id']
    assert route['count'] == 401
    assert route['errors'] == 1
    assert route['timeouts'] == 1
    assert route['latency_ms']['p95'] == 25
    assert snapshot['slow_requests'] == 1
    assert 'secret' not in str(snapshot)
    assert snapshot['recent_requests'][-1]['cause'] == 'TimeoutError'


def test_slow_does_not_fabricate_a_timeout_and_stream_duration_is_excluded():
    metrics = RequestMetrics()
    metrics.record('/api/v1/health', 16000, 200)
    metrics.record('/api/v1/events/stream', 300000, 200)
    snapshot = metrics.snapshot()
    assert snapshot['requests'] == 1
    assert snapshot['by_route']['/api/v1/health']['timeouts'] == 0


def test_http_diagnostics_correlate_responses_and_reject_saturated_workers():
    import http.client
    import json
    import threading

    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.repo import ControlPlaneRepo

    repo = ControlPlaneRepo()
    entered, release = threading.Event(), threading.Event()
    original = repo.list_sessions

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    repo.list_sessions = blocked
    server = ControlPlaneServer(repo, request_workers=1)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    first = http.client.HTTPConnection('127.0.0.1', server.port, timeout=5)
    second = http.client.HTTPConnection('127.0.0.1', server.port, timeout=5)
    try:
        first.request('GET', '/api/v1/sessions')
        assert entered.wait(2)
        second.request('GET', '/api/v1/health')
        rejected = second.getresponse()
        assert rejected.status == 503
        assert rejected.getheader('Retry-After') == '2'
        rejected.read()
        release.set()
        response = first.getresponse()
        assert response.status == 200
        assert response.getheader('X-Request-ID')
        assert response.getheader('Server-Timing').startswith('app;dur=')
        assert json.loads(response.read()) == {'sessions': []}
    finally:
        release.set()
        first.close()
        second.close()
        server.shutdown()
        thread.join(3)


def test_backend_timeout_remains_visible_when_api_returns_an_error_response():
    import http.client
    import threading

    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.repo import ControlPlaneRepo

    repo = ControlPlaneRepo()

    def unavailable(*_args, **_kwargs):
        raise TimeoutError('backend timed out')

    repo.list_sessions = unavailable
    server = ControlPlaneServer(repo)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=5)
    try:
        conn.request('GET', '/api/v1/sessions')
        response = conn.getresponse()
        assert response.status == 500
        request_id = response.getheader('X-Request-ID')
        assert request_id
        response.read()
    finally:
        conn.close()
        server.shutdown()
        thread.join(3)
    route = server.request_metrics.snapshot()['by_route']['/api/v1/sessions']
    assert route['errors'] == 1
    assert route['timeouts'] == 1
    recent = server.request_metrics.snapshot()['recent_requests'][-1]
    assert recent['request_id'] == request_id
    assert recent['cause'] == 'TimeoutError'
