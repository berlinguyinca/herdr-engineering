#!/usr/bin/env python3
"""A throwaway control plane for trying the Rakazo phone app (or the iOS simulator) on this machine.

In-memory repo, Personal Assistant enabled, the Rakazo bridge on, one operator account whose password is random and printed
once. Nothing is persisted, nothing reaches a personal source, and the listener stays on loopback unless --host says otherwise.
In the app: Server -> http://127.0.0.1:<port>, then sign in with the printed email and password.
"""
from __future__ import annotations

import argparse
import os
import secrets
import tempfile
from pathlib import Path

from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def build(email: str, password: str, *, host: str = "127.0.0.1", port: int = 0):
    key = Path(tempfile.mkdtemp(prefix="herdr-local-phone-")) / "apps.key"
    key.write_bytes(os.urandom(32))
    cfg = assistant_config({"personal_assistant": {
        "enabled": True, "apps": {"key_file": str(key), "redirect_base": f"http://{host}"}}})
    bridge = {"enabled": True, "accounts": [{
        "email": email, "password_scrypt": rb.hash_password(password), "role": "operator"}]}
    return ControlPlaneServer(ControlPlaneRepo(), assistant_service=AssistantService(config=cfg), rakazo_bridge=bridge,
                              single_operator=True, host=host, port=port, allow_open=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--email", default="tester@example.com")
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: --host {args.host} exposes an API with NO authentication beyond the printed account to everything that can "
              "reach this machine. Use it only on a network you trust, and stop the server when you are done.", flush=True)
    password = secrets.token_urlsafe(12)
    server = build(args.email, password, host=args.host, port=args.port)
    print(f"server   http://{args.host}:{args.port}\nemail    {args.email}\npassword {password}", flush=True)
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
