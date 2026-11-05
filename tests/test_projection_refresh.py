import asyncio
import threading

import pytest

from herdr_engineering.control_plane import projection_refresh as refresh


def test_refresh_policy_sanitized_and_closed(monkeypatch):
    seen = {}

    class Source:
        recovery_info = {'mode': 'checkpoint', 'frontier': 4, 'dsn': 'secret',
                         'elapsed_ms': 1.2}

        def __init__(self, **kwargs):
            seen.update(kwargs)

        def close(self):
            seen['closed'] = True

    def load(repo, source):
        assert repo._host_aliases == {'alias': 'host'}
        assert repo._service_archive_days == 7
        assert repo.inspector.limits == refresh.ControlPlaneRepo().inspector.limits
        return {'events': 5, 'dsn': 123}

    monkeypatch.setattr(refresh.bridge, 'PostgresSource', Source)
    monkeypatch.setattr(refresh.bridge, 'load_into', load)
    assert refresh.refresh_checkpoint('secret', host_aliases={'alias': 'host'}) == {
        'counts': {'events': 5},
        'recovery_info': {'mode': 'checkpoint', 'frontier': 4, 'elapsed_ms': 1.2}}
    assert seen == {'dsn': 'secret', 'checkpoint_publish': True, 'closed': True}


def test_failure_closes_source(monkeypatch):
    closed = []

    class Source:
        def __init__(self, **kwargs):
            pass

        def close(self):
            closed.append(True)

    def fail(*args):
        raise RuntimeError('secret DSN')

    monkeypatch.setattr(refresh.bridge, 'PostgresSource', Source)
    monkeypatch.setattr(refresh.bridge, 'load_into', fail)
    with pytest.raises(RuntimeError):
        refresh.refresh_checkpoint('secret')
    assert closed == [True]


def test_periodic_nonblocking_nonoverlapping_and_stop(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked(*args, **kwargs):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(refresh, 'refresh_checkpoint', blocked)
    periodic = refresh.ProjectionRefresh('secret', interval=.01)

    async def exercise():
        periodic.start()
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            # Lease/job event-loop work continues while refresh is blocked.
            await asyncio.wait_for(asyncio.sleep(.05), .5)
            assert len(calls) == 1
            assert calls[0] != threading.get_ident()
        finally:
            periodic._stop.set()
            release.set()
            await asyncio.to_thread(periodic.stop)

    asyncio.run(exercise())
    assert not periodic._thread.is_alive()
    assert len(calls) == 1


def test_periodic_retries_and_sanitizes_failures(monkeypatch, caplog):
    retried = threading.Event()
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        if len(calls) >= 2:
            retried.set()
        raise ValueError('postgres://password@secret')

    monkeypatch.setattr(refresh, 'refresh_checkpoint', fail)
    periodic = refresh.ProjectionRefresh('secret', interval=.01)
    periodic.start()
    try:
        assert retried.wait(2)
    finally:
        periodic.stop()
    assert 'ValueError' in caplog.text
    assert 'password' not in caplog.text
    assert 'postgres://' not in caplog.text


@pytest.mark.parametrize('interval', [0, -1, float('nan'), float('inf')])
def test_interval_bounded(interval):
    with pytest.raises(ValueError):
        refresh.ProjectionRefresh('secret', interval=interval)
