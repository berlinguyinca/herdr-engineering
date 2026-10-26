"""Explicit trusted-network snapshot uploads keep the default credential gate."""
import threading
import json
import pytest
from herdr_engineering.control_plane import pi_snapshot
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def test_open_snapshot_requires_opt_in(monkeypatch):
    monkeypatch.delenv('HERDR_CP_INGEST_KEY', raising=False)
    with pytest.raises(ValueError, match='HERDR_CP_INGEST_KEY'):
        pi_snapshot.post_envelopes('http://127.0.0.1:1', None, [])


def test_explicit_open_snapshot_ingests_without_credential(monkeypatch):
    monkeypatch.delenv('HERDR_CP_INGEST_KEY', raising=False)
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        envelopes = pi_snapshot.snapshot_to_envelopes({
            'contractVersion': 3, 'missions': [{'id': 'open-probe', 'title': 'Open probe'}]})
        assert pi_snapshot.post_envelopes(f'http://127.0.0.1:{server.port}', None,
                                          envelopes, allow_open=True) == 1
        assert repo.get_mission('pi:open-probe')['title'] == 'Open probe'
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_explicit_open_snapshot(tmp_path, monkeypatch):
    from herdr_engineering.cli import main
    monkeypatch.delenv('HERDR_CP_INGEST_KEY', raising=False)
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps({'contractVersion': 3,
        'missions': [{'id': 'cli-open-probe', 'title': 'CLI open probe'}]}))
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        assert main(['control-plane', 'feed-pi', '--snapshot', str(snapshot),
                     '--url', f'http://127.0.0.1:{server.port}', '--allow-open']) == 0
        assert repo.get_mission('pi:cli-open-probe')['title'] == 'CLI open probe'
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_polling_ignores_generation_only_changes(tmp_path, monkeypatch):
    from herdr_engineering.cli import main
    import time
    monkeypatch.delenv('HERDR_CP_INGEST_KEY', raising=False)
    snapshot = tmp_path / 'snapshot.json'
    document = {'contractVersion': 3, 'generatedAt': '2026-10-26T00:00:00Z',
                'missions': [{'id': 'quiet-probe', 'title': 'Quiet probe'}]}
    snapshot.write_text(json.dumps(document))
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    real_sleep, waits = time.sleep, []

    def advance(seconds):
        if threading.current_thread() is not threading.main_thread():
            return real_sleep(seconds)
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt
        document['generatedAt'] = '2026-10-26T00:00:30Z'
        snapshot.write_text(json.dumps(document))

    monkeypatch.setattr(time, 'sleep', advance)
    try:
        assert main(['control-plane', 'feed-pi', '--snapshot', str(snapshot), '--interval', '0.1',
                     '--url', f'http://127.0.0.1:{server.port}', '--allow-open']) == 130
        assert len(repo.stream('mission', 'pi:quiet-probe')) == 1
    finally:
        server.shutdown()
        thread.join(timeout=5)
