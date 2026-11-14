"""Command line entry point: ``herdr-artifact-host {public,internal,migrate,healthcheck,import,verify}``.

Every role reads its own configuration slice (``--config``, default the compose secret ``/run/secrets/artifact-host-config``;
see ``host_config``) and refuses a slice rendered for another role.  Nothing is read from the environment.  ``public`` and ``internal`` run until SIGTERM/SIGINT.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .host_config import (
    BLOB_DIR,
    DEFAULT_CONFIG_PATH,
    INTERNAL_PORT,
    PUBLIC_PORT,
    SYNC_DIR,
    HostConfig,
    load,
)

if TYPE_CHECKING:
    from .public_server import PublicConfig

SUBCOMMANDS: dict[str, str] = {
    "public": "serve the public listener",
    "internal": "serve the internal listener",
    "migrate": "apply database migrations (and create the blob directories)",
    "healthcheck": "probe a running role from inside its container (exit 0 when healthy)",
}
IMPORT_COMMANDS = ("import", "verify")  # operator tools, see importer.py
BIND_HOST = "0.0.0.0"  # noqa: S104 - the public listener; the internal listener binds the address in its slice
PROBE_TIMEOUT = 3.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdr-artifact-host")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in SUBCOMMANDS.items():
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
        if name == "healthcheck":
            p.add_argument("role", choices=("public", "internal"))
    from .importer import add_arguments

    add_arguments(sub)
    return parser


def public_config(cfg: HostConfig) -> PublicConfig:
    from .public_server import PublicConfig

    return PublicConfig(
        public_url=cfg.public_url,
        public_host=cfg.public_host,
        mcp_public_hosts=cfg.mcp_public_hosts,
        signing_secret=cfg.claim_key,
        session_seconds=cfg.session_seconds,
        sync_dir=Path(SYNC_DIR),
        pool_id=cfg.pool_id,
        client_ids=cfg.client_ids,
        web_client_id=cfg.web_client_id,
        cognito_domain=cfg.cognito_domain,
        member_signing_secret=cfg.session_secret,
        member_group=cfg.group,
    )


async def _until_signalled() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()


async def run_public(cfg: HostConfig) -> None:
    from .blobs import BlobStore
    from .db import create_pool
    from .library import LibraryStore
    from .public_server import PublicServer, build_app

    pool = await create_pool(cfg.dsn)
    try:
        app = build_app(public_config(cfg), pool, BlobStore(Path(BLOB_DIR)), LibraryStore(pool))
        server = PublicServer(app)
        await server.start(BIND_HOST, PUBLIC_PORT, cfg.health_port)
        try:
            await _until_signalled()
        finally:
            await server.stop()
    finally:
        await pool.close()


async def run_internal(cfg: HostConfig) -> None:
    from .blobs import BlobStore
    from .db import create_pool
    from .internal_server import InternalConfig, InternalServer, build_app

    pool = await create_pool(cfg.dsn)
    try:
        app = build_app(
            InternalConfig(key_digest=cfg.internal_key_digest),
            pool,
            BlobStore(Path(BLOB_DIR)),
        )
        server = InternalServer(app)
        await server.start(cfg.bind, INTERNAL_PORT)
        try:
            await _until_signalled()
        finally:
            await server.stop()
    finally:
        await pool.close()


async def run_migrate(cfg: HostConfig) -> list[str]:
    from .blobs import BlobStore
    from .db import Migrator, create_pool

    BlobStore(Path(BLOB_DIR))  # creates the sha256/ and tmp/ directories
    pool = await create_pool(cfg.dsn)
    try:
        return await Migrator(pool).migrate()
    finally:
        await pool.close()


def probe_url(role: str, cfg: HostConfig) -> str:
    if role == "public":
        return f"http://127.0.0.1:{cfg.health_port}/healthz"
    return f"http://{cfg.bind}:{INTERNAL_PORT}/internal/healthz"


def healthy(role: str, cfg: HostConfig) -> bool:
    try:
        with urllib.request.urlopen(probe_url(role, cfg), timeout=PROBE_TIMEOUT) as r:  # noqa: S310
            return r.status == 200
    except (OSError, urllib.error.URLError):
        return False


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        ns = _parser().parse_args(args)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if ns.command in IMPORT_COMMANDS:
        from .importer import run_command

        return run_command(ns)
    try:
        cfg = load(ns.config)
    except ValueError as exc:  # names keys only, never values
        print(f"herdr-artifact-host {ns.command}: {exc}", file=sys.stderr)
        return 2
    wanted = ns.role if ns.command == "healthcheck" else ns.command
    if cfg.role != wanted:
        print(f"herdr-artifact-host {ns.command}: the config is the {cfg.role} slice, not {wanted}", file=sys.stderr)
        return 2
    if ns.command == "healthcheck":
        return 0 if healthy(ns.role, cfg) else 1
    with contextlib.suppress(KeyboardInterrupt):
        if ns.command == "public":
            asyncio.run(run_public(cfg))
        elif ns.command == "internal":
            asyncio.run(run_internal(cfg))
        else:
            applied = asyncio.run(run_migrate(cfg))
            print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
