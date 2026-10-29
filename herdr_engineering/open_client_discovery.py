"""Discover the operator desktop from live, authenticated HerdR bridge connections.

A server's inherited SSH environment may belong to a long-disconnected client.
Only current bridge processes with a corroborating Tailscale SSH parent contribute
remote candidates. These are host-level attachment evidence, not pane ownership.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

MAX_CLIENTS = 8
DESKTOP_PROBE = r'''
import json, os, platform
from pathlib import Path
system = platform.system()
usable = False
if system == "Darwin":
    usable = Path("/dev/console").stat().st_uid == os.getuid()
elif system == "Linux":
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    usable = bool((os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
                  and runtime and Path(runtime).is_dir()
                  and Path(runtime).stat().st_uid == os.getuid())
print(json.dumps({"usable": usable, "os": system}))
'''


def _arguments(path: Path) -> list[str]:
    return [part.decode(errors='replace') for part in path.read_bytes().split(b'\0') if part]


def _address(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def live_bridges(proc_root: Path = Path('/proc')) -> list[dict]:
    """Read own-UID bridges whose SSH peer is confirmed by a live tailscaled parent."""
    if not proc_root.is_dir() or not hasattr(os, 'getuid'):
        return []
    result = []
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            args = _arguments(process / 'cmdline')
            if not args or Path(args[0]).name != 'herdr' or 'remote-client-bridge' not in args:
                continue
            status = (process / 'status').read_text()
            parent_pid = next(line.split(':', 1)[1].strip() for line in status.splitlines()
                              if line.startswith('PPid:'))
            parent = _arguments(proc_root / parent_pid / 'cmdline')
            if not parent or Path(parent[0]).name != 'tailscaled' or 'ssh' not in parent:
                continue
            parent_ip = next((part.split('=', 1)[1] for part in parent
                              if part.startswith('--remote-ip=')), '')
            parent_ip = _address(parent_ip)
            if not parent_ip:
                continue
            try:
                environment = _arguments(process / 'environ')
                connection = next((entry.split('=', 1)[1] for entry in environment
                                   if entry.startswith('SSH_CONNECTION=')), '')
            except OSError:
                connection = ''
            if connection:
                fields = connection.split()
                if len(fields) != 4 or _address(fields[0]) != parent_ip:
                    continue
            result.append({'ip': parent_ip, 'pid': int(process.name)})
        except (OSError, StopIteration, ValueError):
            # Process exit during traversal is expected.
            continue
    return result


def tailnet_status() -> dict:
    completed = subprocess.run(['tailscale', 'status', '--json'], capture_output=True,
                               text=True, timeout=5, check=True)
    return json.loads(completed.stdout)


def bridge_candidates(bridges: list[dict], status: dict) -> list[dict]:
    """Resolve exact bridge peer IPs, requiring the same tailnet user as this host."""
    own = status.get('Self') or {}
    user = own.get('UserID')
    if not isinstance(user, int) or isinstance(user, bool) or user <= 0:
        return []
    own_ips = set(own.get('TailscaleIPs') or [])
    result = []
    peers = (status.get('Peer') or {}).values()
    for bridge in bridges:
        ip = bridge.get('ip')
        if ip in own_ips:
            continue
        matches = [peer for peer in peers if ip in (peer.get('TailscaleIPs') or [])]
        if len(matches) != 1:
            continue
        peer = matches[0]
        if peer.get('Online') is not True or peer.get('UserID') != user:
            continue
        target = str(peer.get('DNSName') or '').rstrip('.')
        if not target or len(target) > 253 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', target):
            continue
        result.append({'target': target, 'ip': ip, 'bridge_pid': bridge.get('pid')})
    return result


def probe_desktop(target: str) -> dict:
    """Check desktop ownership/capability over existing SSH; never launch an app."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', target):
        return {'usable': False, 'reason': 'invalid-target'}
    try:
        completed = subprocess.run(
            ['ssh', '-oBatchMode=yes', '-oConnectTimeout=3', target,
             'python3 -c ' + shlex.quote(DESKTOP_PROBE)],
            capture_output=True, text=True, timeout=6, check=True)
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise ValueError('invalid probe result')
        return result
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'usable': False, 'reason': 'desktop-probe-unavailable'}


def select_desktop(candidates: list[dict], probe: Callable[[str], dict]) -> dict:
    unique = {candidate['target']: candidate for candidate in candidates}
    if len(unique) > MAX_CLIENTS:
        return {'target': None, 'reason': 'too-many-attached-clients'}
    usable = []
    for target, evidence in unique.items():
        capability = probe(target)
        if capability.get('usable') is True:
            usable.append({**evidence, 'desktop': capability})
    if len(usable) == 1:
        return {**usable[0], 'source': 'live-herdr-bridge'}
    return {'target': None, 'reason': 'ambiguous-desktops' if usable else 'no-usable-desktop'}


def discover_target() -> dict:
    """Discover afresh; unresolved results are diagnostic data, never user questions."""
    bridges = live_bridges()
    if not bridges:
        return {'target': None, 'reason': 'no-live-client-evidence'}
    try:
        status = tailnet_status()
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'target': None, 'reason': 'fabric-discovery-unavailable'}
    return select_desktop(bridge_candidates(bridges, status), probe_desktop)
