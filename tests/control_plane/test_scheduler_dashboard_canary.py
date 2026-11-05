import os
import shutil
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.scheduler_service import SchedulerService
from herdr_engineering.control_plane.sink import DurableEventSink
from tests.control_plane.test_scheduler_postgres import (
    scheduler_postgres as scheduler_database,  # noqa: F401
)


def test_real_dashboard_policy_canary_with_mac_exclusion_and_rollback(request):
    import pytest

    if not shutil.which('node'):
        pytest.skip('Node is required for the browser canary')
    root = Path(__file__).resolve().parents[2]
    if not (root / 'node_modules/playwright/package.json').is_file():
        pytest.skip('Install the declared npm development dependencies for the browser canary')
    dsn = request.getfixturevalue('scheduler_database')
    repo = ControlPlaneRepo(sink=DurableEventSink(dsn=dsn))
    server = ControlPlaneServer(repo, web_root=root / 'packages', api_keys={
        'isolated-canary-operator': 'operator', 'isolated-canary-viewer': 'viewer'})
    linux, mac = 'host-' + 'a' * 32, 'host-' + 'b' * 32
    for name, uid, platform in [('linux-canary', linux, 'linux'), ('mac-canary', mac, 'darwin')]:
        server.scheduler.observe_host(name, {
            'host_uid': uid, 'captured_at': datetime.now(UTC).isoformat(), 'sequence': 1,
            'platform': platform, 'cpu_count': 16, 'cpu_utilization': .1,
            'memory_total_gib': 64, 'memory_available_gib': 50,
            'disk_total_gib': 500, 'disk_free_gib': 400, 'pressure': {},
        })
    repo.set_host_policy('linux-canary', mode='automatic', expected_revision=0,
                         allow_tags=['coding'], deny_tags=['benchmark'])
    for identity, cpu in [('queued-canary', 999), ('reserved-canary', 1)]:
        server.scheduler.reserve({'request_id': identity, 'agent_family': 'codex',
                                  'required_tags': ['coding'],
                                  'resource_profile': {'class': 'coding', 'cpu_cores': cpu}}, 'canary')
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        result = subprocess.run(['node', str(root / 'tests/playwright/scheduler-policy-canary.mjs')],
            cwd=root, capture_output=True, text=True, timeout=90, env={**os.environ,
                'SCHEDULER_CANARY_URL': f'http://127.0.0.1:{server.port}',
                'SCHEDULER_CANARY_OPERATOR': 'isolated-canary-operator',
                'SCHEDULER_CANARY_VIEWER': 'isolated-canary-viewer',
                'SCHEDULER_CANARY_HOST': linux, 'SCHEDULER_CANARY_MAC': mac})
        assert result.returncode == 0, result.stdout + result.stderr
        restored = ControlPlaneRepo(sink=DurableEventSink(dsn=dsn))
        overview = SchedulerService(restored).overview()
        assert next(row for row in overview['hosts'] if row['host_uid'] == linux)['policy']['mode'] == 'automatic'
        policy = restored.get_host('linux-canary')['scheduler_policy']
        assert policy['revision'] == 3 and policy['allow_tags'] == ['coding'] and policy['deny_tags'] == ['benchmark']
        assert next(row for row in overview['hosts'] if row['host_uid'] == mac)['policy']['mode'] == 'manual_only'
    finally:
        server.shutdown()
        thread.join(timeout=5)
