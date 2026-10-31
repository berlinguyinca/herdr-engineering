"""Bridge from the control plane's project keys to a real InferWeave gateway.

The control plane keeps only the SHA-256 of every ``hk-`` key. The gateway's
``auth.client_principals_file`` accepts ``name -> "sha256:<digest>"`` (inferweave PR
"accept pre-hashed principals"), so the digests are handed over as they are and no
plaintext key is ever written next to the gateway. Each key becomes the principal
``herdr:<project>:<key_id>``, which the gateway uses to attribute and prioritise the caller.

The gateway reads that file at startup only, so ``supervise`` restarts it when the key set
changes, at most once per ``debounce`` seconds, and only when the rendered file differs.
Prototype limit: every restart drains in-flight requests (up to the gateway's own grace).
The fix is a live reload in the gateway, not a faster restart here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .llm_proxy import ControlPlaneClient

log = logging.getLogger("herdr.llm_gateway_bridge")

_NAME_OK = re.compile(r"^[A-Za-z0-9\-_.:/]{1,128}$")
_DIGEST_OK = re.compile(r"^[0-9a-f]{64}$")


def render_principals(config: dict) -> dict[str, str]:
    """``principal name -> sha256:<digest>`` for every active key; malformed entries are
    skipped, never passed on, so one bad project name cannot take the file down."""
    out: dict[str, str] = {}
    for key in config.get("keys", []):
        name = f"herdr:{key.get('project', '')}:{key.get('key_id', '')}"
        digest = str(key.get("key_hash", ""))
        if not _NAME_OK.match(name) or not _DIGEST_OK.match(digest):
            log.warning("skipping key %r: not a valid principal", key.get("key_id"))
            continue
        out[name] = "sha256:" + digest
    return out


def write_if_changed(path: Path, principals: dict[str, str]) -> bool:
    """Atomically write ``principals`` as a 0600 JSON file; False when already current."""
    body = json.dumps(principals, sort_keys=True, indent=1) + "\n"
    try:
        if path.read_text() == body:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".principals-")
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return True


def sync_once(control: ControlPlaneClient, path: Path) -> bool | None:
    """One pass. None when the control plane is unreachable: the last file keeps serving,
    exactly as the python proxy keeps its last key table."""
    try:
        config = control.config()
    except Exception as exc:  # noqa: BLE001
        log.warning("control plane config unavailable: %s", exc)
        return None
    return write_if_changed(path, render_principals(config))


def supervise(control: ControlPlaneClient, path: Path, command: list[str], *,
              interval: float = 5.0, debounce: float = 30.0, sleep=time.sleep,
              clock=time.monotonic, stop=lambda: False) -> int:
    """Keep ``command`` (the gateway) running against the current principals file."""
    sync_once(control, path)  # the file must exist before the gateway starts
    child = subprocess.Popen(command)  # noqa: S603
    last_restart = clock()
    pending = False
    try:
        while not stop():
            sleep(interval)
            code = child.poll()
            if code is not None:
                log.error("gateway exited with %s", code)
                return code or 1
            if sync_once(control, path):
                pending = True
            if pending and clock() - last_restart >= debounce:
                log.info("key set changed: restarting the gateway")
                child.send_signal(signal.SIGTERM)
                child.wait()
                child = subprocess.Popen(command)  # noqa: S603
                last_restart, pending = clock(), False
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            child.wait()
    return 0
