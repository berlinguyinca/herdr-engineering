"""``herdr-eng google status|set-client|connect|disconnect``: set up and re-set up the assistant's Google connection.

Everything goes through the control plane's existing ``/api/v1/assistant-apps`` routes, the same ones the dashboard uses, so
there is one code path. The client secret is read from Google's downloaded ``client_secret_*.json``, a file, or a hidden
prompt, never from an argument, and it is never printed. ``status`` also prints the Google Cloud Console steps that
cannot be automated and checks the parts that can (the redirect URI Google must list, whether the callback answers).
"""
from __future__ import annotations

import getpass
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CALLBACK_PATH = "/api/v1/assistant-apps/google/callback"
TIMEOUT = 20

CONSOLE_STEPS = (
    "Google Cloud Console (console.cloud.google.com), the project that owns the OAuth client:",
    "  1. APIs & Services > Library: enable the Google Calendar API (Gmail is not requested).",
    "  2. APIs & Services > Credentials: the client type is 'Web application' and 'Authorized redirect URIs' lists exactly:",
    "       {redirect}",
    "  3. OAuth consent screen > Data Access: the scopes are exactly openid, .../auth/userinfo.email,",
    "     .../auth/calendar.readonly and .../auth/calendar.events (calendar is 'sensitive': verification, no paid assessment).",
    "     Audience: add every Google account that will connect under 'Test users' (while Testing).",
    "     Testing mode expires refresh tokens after 7 days: set the publishing status to 'In production' to keep them.",
    "     Google still shows 'Google hasn't verified this app' (click Advanced > Go to <app>); personal use needs no review.",
    "  4. herdr-eng google set-client --json-file ~/Downloads/client_secret_....json   (Credentials > Download JSON)",
    "  5. herdr-eng google connect   and open the printed link in a browser on the tailnet.",
)


class SetupError(Exception):
    pass


class Api:
    def __init__(self, base: str, key: str | None = None, opener=urllib.request.urlopen):
        self.base, self.key, self._open = base.rstrip("/"), key, opener

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        headers = {"accept": "application/json"}
        if self.key:
            headers["X-API-Key"] = self.key
        data = None
        if body is not None:
            data, headers["content-type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)  # noqa: S310
        try:
            with self._open(req, timeout=TIMEOUT) as resp:
                return resp.status, _json(resp.read(1 << 20))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, _json(exc.read(1 << 20))
            finally:
                exc.close()
        except (urllib.error.URLError, OSError) as exc:
            raise SetupError(f"cannot reach the control plane at {self.base}: {getattr(exc, 'reason', exc)}") from None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def read_client_json(path: Path) -> tuple[str, str, list[str]]:
    """(client_id, client_secret, redirect_uris) from Google's download. A 'web' client is required: a desktop
    ('installed') client has no redirect URI the dashboard can use."""
    try:
        raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupError(f"cannot read {path}: {getattr(exc, 'strerror', None) or 'not valid JSON'}") from None
    if not isinstance(raw, dict) or "installed" in raw and "web" not in raw:
        raise SetupError("this is a Desktop/'installed' client; create a 'Web application' client in Credentials instead")
    web = raw.get("web")
    if not isinstance(web, dict) or not web.get("client_id") or not web.get("client_secret"):
        raise SetupError("the file has no web.client_id / web.client_secret (download the JSON of a Web application client)")
    uris = web.get("redirect_uris")
    return str(web["client_id"]), str(web["client_secret"]), [u for u in uris if isinstance(u, str)] if isinstance(uris, list) else []


def _redirect(api: Api) -> tuple[bool, str, dict]:
    status, data = api.call("GET", "/api/v1/assistant-apps")
    if status != 200 or not isinstance(data, dict):
        raise SetupError(f"the control plane answered {status} for the assistant apps status")
    return bool(data.get("ready")), str(data.get("redirect_uri") or ""), data


def cmd_status(api: Api, out=print) -> int:
    ready, redirect, data = _redirect(api)
    google = [p for p in data.get("providers", []) if p.get("provider") == "google"]
    configured = any(p.get("client_configured") for p in google)
    connected = [p for p in google if p.get("connected")]
    problems = 0

    def line(ok: bool, text: str) -> None:
        nonlocal problems
        problems += (not ok)
        out(("ok   " if ok else "FIX  ") + text)

    line(ready, "assistant apps are set up on the control plane" if ready else
         "assistant apps are not set up: set control_plane.personal_assistant.apps (key_file, redirect_base) and redeploy")
    line(redirect.startswith("https://"), f"redirect URI Google must list: {redirect or '(none)'}")
    if redirect:
        status, _ = api.call("GET", CALLBACK_PATH)      # no code/state: a 302 back to the dashboard proves the route answers
        line(status in (200, 302, 400), f"the callback route answers ({status})")
    line(configured, "an OAuth client is stored" if configured else "no OAuth client stored yet: herdr-eng google set-client")
    if connected:
        for p in connected:
            line(p.get("status") == "connected", f"{p.get('name')}: {p.get('account') or '?'} is {p.get('status')}")
    else:
        line(False, "no Google account is connected yet: herdr-eng google connect")
    if problems:
        out("")
        out("\n".join(CONSOLE_STEPS).format(redirect=redirect or "<redirect URI above>"))
    return 1 if problems else 0


def cmd_set_client(api: Api, json_file: str | None, client_id: str | None, secret_file: str | None, out=print,
                   prompt=getpass.getpass) -> int:
    uris: list[str] = []
    if json_file:
        cid, secret, uris = read_client_json(Path(json_file))
    else:
        cid = client_id or ""
        if secret_file:
            try:
                secret = Path(secret_file).expanduser().read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SetupError(f"cannot read {secret_file}: {exc.strerror}") from None
        elif sys.stdin.isatty():
            secret = prompt("Google client secret (hidden): ").strip()
        else:
            secret = sys.stdin.readline().strip()
        if not cid or not secret:
            raise SetupError("give --json-file, or --client-id with --client-secret-file (or type the secret at the prompt)")
    _, redirect, _ = _redirect(api)
    if uris and redirect and redirect not in uris:
        out(f"warning: the client lists {', '.join(uris) or 'no redirect URI'} but the dashboard uses {redirect}.")
        out("         Add that exact URI under Credentials > Authorized redirect URIs or Google will refuse the sign-in.")
    status, data = api.call("PUT", "/api/v1/assistant-apps/google/client", {"client_id": cid, "client_secret": secret})
    if status != 200:
        raise SetupError(f"the control plane refused the client ({status}: {data.get('error', 'failed') if isinstance(data, dict) else 'failed'})")
    out("stored the Google OAuth client (the secret is encrypted at rest and never shown again).")
    out("next: herdr-eng google connect")
    return 0


def cmd_connect(api: Api, out=print) -> int:
    status, data = api.call("POST", "/api/v1/assistant-apps/google/connect", {})
    if status == 409:
        raise SetupError("no OAuth client is stored yet: run  herdr-eng google set-client")
    if status != 200 or not isinstance(data, dict) or not data.get("url"):
        raise SetupError(f"could not start the sign-in ({status})")
    out("Open this link in a browser that can reach the dashboard (the tailnet), sign in and approve:")
    out(data["url"])
    out("It expires in a few minutes. Afterwards: herdr-eng google status")
    return 0


def cmd_disconnect(api: Api, yes: bool, out=print) -> int:
    if not yes:
        raise SetupError("this signs the assistant out of Google (the stored tokens are revoked); repeat with --yes to confirm")
    status, _ = api.call("DELETE", "/api/v1/assistant-apps/google")
    if status != 200:
        raise SetupError(f"disconnect failed ({status})")
    out("disconnected. The stored OAuth client stays; run  herdr-eng google connect  to sign in again.")
    return 0


def run(args) -> int:
    from .host_config import HostConfigUnavailable, exit_unavailable, resolve_control_plane
    try:
        base = resolve_control_plane(args.control_plane)
    except HostConfigUnavailable as exc:
        return exit_unavailable(exc)
    key = None
    if args.api_key_file:
        try:
            key = Path(args.api_key_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"cannot read --api-key-file: {exc.strerror}", file=sys.stderr)
            return 2
    api = Api(base, key)
    try:
        if args.google_command == "status":
            return cmd_status(api)
        if args.google_command == "set-client":
            return cmd_set_client(api, args.json_file, args.client_id, args.client_secret_file)
        if args.google_command == "connect":
            return cmd_connect(api)
        return cmd_disconnect(api, args.yes)
    except SetupError as exc:
        print(f"herdr-eng google: {exc}", file=sys.stderr)
        return 1


def register(sub) -> None:
    p = sub.add_parser("google", help="set up the assistant's Google (Gmail/Calendar) connection: status, set-client, "
                       "connect, disconnect")
    p.add_argument("--control-plane", default=None, help="control-plane base URL (default: host.json)")
    p.add_argument("--api-key-file", default=None, help="PATH to an API key file (only needed when the control plane "
                   "requires keys; the open dashboard listener needs none)")
    g = p.add_subparsers(dest="google_command", required=True)
    g.add_parser("status", help="check the setup and print what is still to do, including the Google Console steps")
    s = g.add_parser("set-client", help="store (or replace) the Google OAuth client")
    s.add_argument("--json-file", default=None, help="Google's downloaded client_secret_*.json (Web application)")
    s.add_argument("--client-id", default=None)
    s.add_argument("--client-secret-file", default=None, help="PATH to a file holding the secret (never the secret itself)")
    g.add_parser("connect", help="print the link that signs a Google account in")
    d = g.add_parser("disconnect", help="sign the assistant out of Google")
    d.add_argument("--yes", action="store_true", help="confirm")
    p.set_defaults(func=run)
