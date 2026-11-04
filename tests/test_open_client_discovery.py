"""Client discovery uses active authenticated bridge evidence, never peer ordering."""

from herdr_engineering import open_client_discovery as discovery


def peer(ip, name, *, user=7, online=True, system='macOS'):
    return {'TailscaleIPs': [ip], 'DNSName': name + '.tail.example.',
            'UserID': user, 'Online': online, 'OS': system}


def fabric(peers):
    return {'Self': {'UserID': 7, 'TailscaleIPs': ['100.64.0.1']},
            'Peer': {str(i): value for i, value in enumerate(peers)}}


def test_exact_live_bridge_ip_wins_over_first_mac():
    status = fabric([peer('100.64.0.2', 'other'), peer('100.64.0.3', 'actual')])
    candidates = discovery.bridge_candidates([{'ip': '100.64.0.3', 'pid': 50}], status)
    assert [item['target'] for item in candidates] == ['actual.tail.example']


def test_no_bridge_never_selects_online_mac():
    assert discovery.bridge_candidates([], fabric([peer('100.64.0.2', 'mac')])) == []


def test_peer_must_be_online_and_same_principal():
    status = fabric([peer('100.64.0.2', 'foreign', user=8),
                     peer('100.64.0.3', 'offline', online=False)])
    assert discovery.bridge_candidates([{'ip': '100.64.0.2'}, {'ip': '100.64.0.3'}], status) == []
    status['Self'].pop('UserID')
    assert discovery.bridge_candidates([{'ip': '100.64.0.2'}], status) == []


def test_selects_only_usable_desktop_and_deduplicates_connections():
    candidates = [{'target': 'server', 'ip': '100.64.0.2'},
                  {'target': 'mac', 'ip': '100.64.0.3'},
                  {'target': 'mac', 'ip': '100.64.0.3'}]
    probed = []

    def probe(target):
        probed.append(target)
        return {'usable': target == 'mac', 'os': 'Darwin' if target == 'mac' else 'Linux'}

    result = discovery.select_desktop(candidates, probe)
    assert result['target'] == 'mac'
    assert probed == ['server', 'mac']
    assert result['source'] == 'live-herdr-bridge'


def test_multiple_usable_clients_are_unresolved_without_question():
    result = discovery.select_desktop([{'target': 'one'}, {'target': 'two'}],
                                     lambda _: {'usable': True})
    assert result['target'] is None
    assert result['reason'] == 'ambiguous-desktops'


def write_bridge(root, *, ip='100.64.0.3', parent_ip='100.64.0.3', bridge=True):
    process = root / '123'
    process.mkdir(parents=True)
    command = ['herdr', 'remote-client-bridge'] if bridge else ['bash']
    (process / 'cmdline').write_bytes(b'\0'.join(value.encode() for value in command))
    (process / 'environ').write_bytes(f'SSH_CONNECTION={ip} 40000 100.64.0.1 22\0'.encode())
    (process / 'status').write_text('Name:\therdr\nPPid:\t122\n')
    parent = root / '122'
    parent.mkdir()
    (parent / 'cmdline').write_bytes(f'/usr/sbin/tailscaled\0be-child\0ssh\0--remote-ip={parent_ip}\0'.encode())
    return process


def test_proc_bridge_requires_authenticated_parent_corroboration(tmp_path):
    write_bridge(tmp_path)
    assert discovery.live_bridges(tmp_path) == [{'ip': '100.64.0.3', 'pid': 123}]
    (tmp_path / '122' / 'cmdline').write_bytes(b'/usr/sbin/tailscaled\0be-child\0ssh\0--remote-ip=100.64.0.9\0')
    assert discovery.live_bridges(tmp_path) == []


def test_inherited_agent_ssh_environment_is_not_enough(tmp_path, monkeypatch):
    monkeypatch.setenv('SSH_CONNECTION', '100.64.0.3 40000 100.64.0.1 22')
    write_bridge(tmp_path, bridge=False)
    assert discovery.live_bridges(tmp_path) == []


def test_discovery_uses_live_bridge_and_capability_without_open(monkeypatch):
    monkeypatch.setattr(discovery, 'live_bridges', lambda: [{'ip': '100.64.0.3', 'pid': 123}])
    monkeypatch.setattr(discovery, 'tailnet_status', lambda: fabric([peer('100.64.0.3', 'actual')]))
    monkeypatch.setattr(discovery, 'probe_desktop', lambda target: {'usable': True, 'os': 'Darwin'})
    assert discovery.discover_target()['target'] == 'actual.tail.example'


def test_inherited_gui_capability_never_proves_local_attachment(monkeypatch):
    monkeypatch.setattr(discovery, 'live_bridges', lambda: [])
    monkeypatch.setenv('SSH_CONNECTION', '100.64.0.3 40000 100.64.0.1 22')
    monkeypatch.setenv('DISPLAY', ':0')
    assert discovery.discover_target()['target'] is None
    monkeypatch.setattr(discovery, 'live_bridges', lambda: [{'ip': '100.64.0.3'}])
    monkeypatch.setattr(discovery, 'tailnet_status', lambda: fabric([]))
    assert discovery.discover_target()['target'] is None


def test_too_many_clients_does_not_probe_or_guess():
    def unexpected_probe(_):
        raise AssertionError('capacity overflow must not probe any client')

    result = discovery.select_desktop([{'target': str(i)} for i in range(9)], unexpected_probe)
    assert result['target'] is None
    assert result['reason'] == 'too-many-attached-clients'


def test_parent_connection_is_used_when_bridge_environment_unavailable(tmp_path):
    process = write_bridge(tmp_path)
    (process / 'environ').unlink()
    assert discovery.live_bridges(tmp_path) == [{'ip': '100.64.0.3', 'pid': 123}]


def test_fabric_failure_never_falls_back_to_execution_desktop(monkeypatch):
    monkeypatch.setattr(discovery, 'live_bridges', lambda: [{'ip': '100.64.0.3'}])

    def unavailable():
        raise OSError('not available')

    monkeypatch.setattr(discovery, 'tailnet_status', unavailable)
    assert discovery.discover_target() == {'target': None, 'reason': 'fabric-discovery-unavailable'}


def test_probe_rejects_unsafe_host_without_spawning(monkeypatch):
    monkeypatch.setattr(discovery.subprocess, 'run', lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError('must not spawn')))
    assert discovery.probe_desktop('-oProxyCommand=evil')['usable'] is False


def test_headless_local_session_is_unresolved(monkeypatch):
    monkeypatch.setattr(discovery, 'live_bridges', lambda: [])
    monkeypatch.delenv('DISPLAY', raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    assert discovery.discover_target() == {'target': None, 'reason': 'no-live-client-evidence'}
