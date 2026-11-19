"""Per-host maintenance companion; runs only while the local Herdr server lives."""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
from pathlib import Path

log = logging.getLogger(__name__)


def server_running(proc_root: Path = Path('/proc')) -> bool:
    """Recognize this user's server, not Herdr clients or remote bridges."""
    try:
        for process in proc_root.iterdir():
            if not process.name.isdigit():
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue
                args = (process / 'cmdline').read_bytes().split(b'\0')
                if (args and Path(os.fsdecode(args[0])).name == 'herdr'
                        and b'server' in args[1:]):
                    return True
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return False


def watch(cleanup, *, interval: float, stop: threading.Event,
          is_running=server_running, wait=None) -> None:
    """Keep individual scan failures from taking down the companion."""
    if interval <= 0:
        raise ValueError('cleanup interval must be positive')
    wait = wait or stop.wait
    while not stop.is_set():
        if is_running():
            try:
                result = cleanup()
                log.info('build cleanup: %s', json.dumps(result, sort_keys=True))
            except Exception:
                log.exception('build cleanup failed; retrying next interval')
        wait(interval)


def policy_kwargs(cfg: dict) -> dict:
    """``run_once`` policy values from a ``build_cleanup`` config block (defaults match config.DEFAULTS)."""
    return {
        "high_water": float(cfg.get('high_water_percent', 85)),
        "low_water": float(cfg.get('low_water_percent', 80)),
        "min_age_hours": float(cfg.get('min_age_hours', 24)),
        "pressure_water": float(cfg.get('pressure_percent', 90)),
        "pressure_min_age_hours": float(cfg.get('pressure_min_age_hours', 2)),
    }


def _tier_bases() -> list[Path]:
    """Parent directory of every enrolled storage tier's cache dir (managed targets live beside it).

    A ``volume_uuid`` tier's directory is relative to its mount, exactly as the build resolves it.
    """
    from .build_cache_config import validate
    from .build_cache_location import mounted_volume
    from .config import load_config
    section = load_config().get("fabric_build_cache")
    bases = []
    for tier in validate(section).local.storage_tiers if section else ():
        if tier.volume_uuid:
            mount = mounted_volume(tier.volume_uuid)
            if mount is not None:
                bases.append((mount / tier.directory).parent)
        else:
            bases.append(Path(tier.directory).expanduser().parent)
    return bases


def _managed_output_dirs() -> list[Path]:
    """Existing legacy pools and per-workspace target folders of the state dir and every storage tier."""
    from .build_coord import _default_state_dir
    from .cargo_target_storage import shared_targets_root
    bases = [_default_state_dir()]
    try:
        bases += _tier_bases()
    except Exception:  # noqa: BLE001 - a config problem must not stop the sweep of the default dirs
        log.warning("build cleanup: storage tiers unreadable; sweeping the default state dir only",
                    exc_info=True)
    found = []
    for base in bases:
        for path in (shared_targets_root(base), base / "targets"):
            real = Path(os.path.realpath(path))
            if real.is_dir() and real not in found:
                found.append(real)
    return found


def effective_roots(cfg: dict, explicit: list[str] | None = None) -> list[Path]:
    """Configured roots (or explicit ones) plus every managed target folder that exists right now.

    Explicit and configured roots are returned as given (only ``~`` expanded) so the sweep's
    canonical-root check still sees a symlinked root; managed folders are resolved because they
    are derived from state locations. Recomputed by the watcher on every cycle, so a pool created
    after it started is picked up.
    """
    roots = list(explicit if explicit is not None else cfg.get('roots', []))
    paths = [Path(root).expanduser() for root in roots]
    if explicit is None:
        # Default roots may not exist on every host (e.g. no ~/work); explicit ones must.
        paths = [path for path in paths if path.is_dir()]
        paths += [path for path in _managed_output_dirs() if path not in paths]
    return paths


def cleanup_cycle(cfg: dict, explicit: list[str] | None, state_dir: Path, dry_run: bool = False) -> dict:
    """One sweep. Roots are re-evaluated every cycle: pools appear after the watcher has started."""
    from .build_cleanup import run_once
    return run_once(roots=effective_roots(cfg, explicit), state_dir=state_dir, dry_run=dry_run,
                    **policy_kwargs(cfg))


def command(args) -> int:
    from .build_coord import _default_state_dir
    from .config import load_config

    cfg = load_config().get('build_cleanup', {})
    state_dir = _default_state_dir()
    if args.cleanup_command == 'status':
        status = state_dir / 'build-cleanup.json'
        print(status.read_text() if status.exists() else json.dumps({'status': 'never_run'}))
        return 0
    roots = effective_roots(cfg, args.root)
    if not roots:
        print(json.dumps({'error': 'configure build_cleanup.roots or pass --root'}))
        return 2
    def cleanup():
        return cleanup_cycle(cfg, args.root, state_dir, args.dry_run)
    if args.cleanup_command == 'run':
        if not cfg.get('enabled', True):
            print(json.dumps({'status': 'disabled', 'message': 'build_cleanup.enabled is false'}))
            return 0
        result = cleanup()
        skipped = [w for w in result.get('warnings', []) if w.startswith('skipped root')]
        if args.root and skipped:  # a root the operator named must work; only default roots are skipped
            result['status'] = 'error'
            result['errors'] = [*result.get('errors', []), *skipped]
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if result.get('status') == 'error' else 0
    if not cfg.get('enabled', True):
        print(json.dumps({'status': 'disabled', 'message': 'set build_cleanup.enabled: true'}))
        return 0
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: stop.set())
    watch(cleanup, interval=float(cfg.get('interval_seconds', 900)), stop=stop)
    return 0
