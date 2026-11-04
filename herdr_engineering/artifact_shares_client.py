"""`herdr-eng artifacts share`: create, review, approve and revoke artifact share links.

Approve is the only step that exposes anything, and it prints exactly what will be exposed first. The link comes
back once, from the approve response. Human output only renders keys from the allow-lists below, so a token or
token hash the server should never send cannot reach the terminal; ``--json`` / list / events pass the server's
JSON through.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
from urllib.parse import quote

from .artifact_reports_client import ReportClient, ReportClientError

MAX_DAYS = 3650
SHOWN_FILES = 20
SHARE_KEYS = ("share_id", "artifact_id", "version_id", "audience", "state", "allow_emails", "allow_groups",
              "expires_at", "entry", "title", "created_at", "created_by", "approved_at", "revoked_at")
EMAIL = re.compile(r"^[^@\s,\"']+@[^@\s,\"']+$")


class ShareError(ReportClientError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class PoolCheckSkipped(Exception):
    pass


REQUEST_TIMEOUT = 120   # seconds; an approve copies the whole bundle before it answers


class ShareClient(ReportClient):
    """ReportClient transport that keeps the server's short JSON error message and status."""

    def request(self, path, body=None):
        from urllib.request import Request
        headers = {**self.headers, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        req = Request(self.base + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        try:
            with self.opener.open(req, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            message = None
            try:
                parsed = json.loads(error.read(65536))
                if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
                    message = parsed["error"][:300]
            except (ValueError, OSError):
                pass
            raise ShareError(message or "request failed", error.code) from None
        except (OSError, ValueError):
            raise ShareError("control plane is unreachable") from None
        try:
            return _strip_secrets(json.loads(raw))
        except ValueError:
            raise ShareError("control plane did not return structured JSON") from None


def _path(aid, sid=None, tail=None):
    out = "/api/v1/artifacts/" + quote(aid, safe="") + "/shares"
    if sid is not None:
        out += "/" + quote(sid, safe="")
    return out + ("/" + tail if tail else "")


def _size(n):
    if not isinstance(n, int):
        return "unknown size"
    for power, unit in enumerate(("B", "KiB", "MiB", "GiB")):
        if n < 1024 ** (power + 1) or unit == "GiB":
            return f"{n} B" if power == 0 else f"{n / 1024 ** power:.1f} {unit}"


def _strip_secrets(value):
    """Defence in depth: the server never sends these, and the terminal never shows them if it did."""
    if isinstance(value, dict):
        return {k: _strip_secrets(v) for k, v in value.items() if k not in {"token", "token_hash"}}
    if isinstance(value, list):
        return [_strip_secrets(v) for v in value]
    return value


_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def _safe(value, limit=200):
    """Server-derived text for a terminal: control and bidi characters become visible escapes, length is capped."""
    text = str(value)
    text = _UNSAFE.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 256 else f"\\u{ord(m.group()):04x}", text)
    return text if len(text) <= limit else text[:limit] + "..."


def _clean_share(share):
    return {k: share[k] for k in SHARE_KEYS if isinstance(share, dict) and k in share}


def render_summary(share, summary):
    s = _clean_share(share)
    lines = [f"Share {_safe(s.get('share_id', '?'))} ({_safe(s.get('state', '?'))}) of artifact "
             f"{_safe(s.get('artifact_id', '?'))} version {_safe(s.get('version_id', '?'))}"]
    if s.get("title"):
        lines.append(f"  title: {_safe(s['title'])}")
    if summary:
        lines += [f"  audience: {_safe(summary.get('audience', s.get('audience')))}"]
        recipients = (s.get("allow_emails") or []) + [f"group:{g}" for g in s.get("allow_groups") or []]
        if recipients:
            lines.append("  allowed: " + ", ".join(_safe(r) for r in recipients))
        lines += [f"  entry: {_safe(summary.get('entry'))}",
                  f"  exposes: {_safe(summary.get('file_count'))} files, {_safe(_size(summary.get('total_bytes')))}",
                  f"  expires: {_safe(summary.get('expires_at') or 'never')}"]
        for warning in summary.get("warnings") or []:
            lines.append(f"  WARNING: {_safe(warning, 400)}")
        files = summary.get("files") or []
        for f in files[:SHOWN_FILES]:
            if isinstance(f, dict):
                lines.append(f"    {_safe(f.get('name'))}  ({_safe(_size(f.get('size_bytes')))})")
        total = summary.get("file_count") if isinstance(summary.get("file_count"), int) else len(files)
        if total > min(len(files), SHOWN_FILES):
            lines.append(f"    ... and {total - min(len(files), SHOWN_FILES)} more")
    return "\n".join(lines)


def _err(message):
    print(f"herdr-eng: {_safe(message, 400)}", file=sys.stderr)


def _status_message(error):
    if error.status == 409:
        return f"conflict: this share was already approved or revoked ({error})"
    if error.status == 410:
        return f"this share has expired and can no longer be approved ({error})"
    return str(error)


def _cognito_users_exist(pool_id, emails):
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise PoolCheckSkipped("boto3 is not installed") from None
    try:
        session = boto3.session.Session()
        if session.get_credentials() is None:
            raise PoolCheckSkipped("no AWS credentials available")
        client = session.client("cognito-idp", region_name=pool_id.split("_", 1)[0])
        found = {}
        for email in emails:
            reply = client.list_users(UserPoolId=pool_id, Filter=f'email = "{email}"', Limit=1)
            found[email] = bool(reply.get("Users"))
        return found
    except PoolCheckSkipped:
        raise
    except (BotoCoreError, ClientError) as error:
        raise PoolCheckSkipped(f"AWS lookup failed ({type(error).__name__})") from None


def _pool_id(args):
    if args.pool_id:
        return args.pool_id
    if args.fleet_config:
        try:
            from pathlib import Path
            from .fleet_config import load_fleet_config
            return load_fleet_config(Path(args.fleet_config)).section("control_plane")["collab"]["cognito_pool"]
        except Exception:
            return None
    return None


def _check_pool(args, emails):
    pool = _pool_id(args)
    if not pool:
        _err("pool check skipped: no --pool-id (or readable --fleet-config with control_plane.collab.cognito_pool)")
        return
    try:
        found = _cognito_users_exist(pool, [e.lower() for e in emails])
    except PoolCheckSkipped as why:
        _err(f"pool check skipped: {why}")
        return
    except Exception as why:  # a lookup problem must never fail the command
        _err(f"pool check skipped: {type(why).__name__}")
        return
    for email in emails:
        if not found.get(email.lower()):
            _err(f"warning: no Cognito account found for {email}; they must sign up before they can open the link")


def _validate_create(args):
    emails, groups = args.email or [], args.group or []
    if args.room and args.audience != "lab":
        return "--room only applies to --audience lab"
    if args.audience == "lab" and not (emails or groups or args.room):
        return "audience lab needs at least one --email, --group or --room"
    if args.audience == "public" and (emails or groups):
        return "audience public cannot have --email/--group; use --audience lab to restrict recipients"
    for e in emails:
        if not EMAIL.match(e):
            return f"not a valid email address: {e}"
    if args.expires_in_days is not None and not 1 <= args.expires_in_days <= MAX_DAYS:
        return f"--expires-in-days must be an integer from 1 to {MAX_DAYS}"
    return None


def _say_stream(args):
    return sys.stderr if getattr(args, "json", False) else sys.stdout


def _say(args, text):
    print(text, file=_say_stream(args))


def _confirmed(args):
    if args.yes:
        return True
    try:
        interactive = bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        interactive = False
    if not interactive:
        _err("refusing to approve without confirmation: stdin is not a terminal; re-run with --yes "
             "after reviewing the summary above")
        return False
    print("Type 'yes' to expose exactly the files above: ", end="", file=_say_stream(args), flush=True)
    if sys.stdin.readline().strip().lower() == "yes":
        return True
    _err("not approved")
    return False


def run_command(args):
    action = args.share_action
    if action == "create" and (problem := _validate_create(args)):
        _err(problem)
        return 2
    if action == "events" and args.limit is not None and args.limit < 1:
        _err("--limit must be at least 1")
        return 2
    key = os.environ.get(args.api_key_env)
    try:
        from .host_config import resolve_control_plane
        client = ShareClient(resolve_control_plane(args.control_plane), api_key=key)
        aid = args.artifact_id
        if action == "create":
            body = {"version_id": args.version_id, "audience": args.audience}
            if args.email:
                body["allow_emails"] = args.email
            if args.group:
                body["allow_groups"] = args.group
            if args.room:
                body["room_id"] = args.room
            if args.expires_in_days is not None:
                body["expires_in_days"] = args.expires_in_days
            if args.entry:
                body["entry"] = args.entry
            if args.title:
                body["title"] = args.title
            if args.check_pool and args.email:
                try:
                    _check_pool(args, args.email)
                except Exception:
                    _err("pool check skipped: unexpected error")
            result = client.request(_path(aid), body)
            if args.json:
                print(json.dumps(result))
            else:
                print(render_summary(result.get("share", {}), result.get("summary")))
                print(f"Not live yet. Review, then: herdr-eng artifacts share approve {_safe(aid)} "
                      f"{_safe(result.get('share', {}).get('share_id', '<id>'))}")
        elif action == "show":
            result = client.request(_path(aid, args.share_id))
            if args.json:
                print(json.dumps(result))
            else:
                print(render_summary(result.get("share", {}), result.get("summary")))
                if result.get("summary_error"):
                    print(f"  cannot be approved as is: {_safe(result['summary_error'], 400)}")
        elif action == "list":
            print(json.dumps(client.request(_path(aid))))
        elif action == "events":
            result = client.request(_path(aid, args.share_id, "events"))
            if args.limit is not None and isinstance(result.get("events"), list):
                result["events"] = result["events"][-args.limit:]
            print(json.dumps(result))
        elif action == "revoke":
            result = client.request(_path(aid, args.share_id, "revoke"), {})
            if args.json:
                _say(args, f"Share {_safe(args.share_id)} is revoked (revoking again is harmless); its link no longer works.")
                print(json.dumps(result))
            else:
                print(f"Share {_safe(args.share_id)} is revoked (revoking again is harmless); its link no longer works.")
        elif action == "approve":
            current = client.request(_path(aid, args.share_id))
            share = current.get("share", {})
            if share.get("state") != "pending":
                _err(f"share is {share.get('state')}, not pending; nothing to approve")
                return 1
            _say(args, render_summary(share, current.get("summary")))
            if not current.get("summary"):
                _err(f"cannot approve: {current.get('summary_error') or 'no exposure summary available'}")
                return 1
            if not _confirmed(args):
                return 1
            try:
                result = client.request(_path(aid, args.share_id, "approve"), {"confirm": True})
            except ShareError as error:
                if error.status is not None:
                    raise
                # timeout or lost connection: the server may have finished the approval without us seeing it
                _err(f"no answer to the approve request (timed out or the connection dropped); the share MAY "
                     "already be live. Check with: "
                     f"herdr-eng artifacts share show {aid} {args.share_id} (revoke it if unwanted: "
                     f"herdr-eng artifacts share revoke {aid} {args.share_id}). The link is shown only once, so a "
                     "share that went live this way needs a revoke and a new share to get a link.")
                return 1
            link = result.get("url")
            if (not isinstance(link, str) or not link.startswith(("https://", "http://"))
                    or _UNSAFE.search(link) or any(c.isspace() for c in link)):
                _err("approved, but the server returned no usable link; revoke this share and create a new one")
                return 1
            if args.json:
                _say(args, "Share is LIVE. The link is in the JSON on stdout and will not be shown again.")
                print(json.dumps(result))
            else:
                print("\nShare is LIVE. Anyone holding this link can open it; treat it like a password.")
                print(f"\n    {link}\n")
                print("This link will not be shown again. Copy it now; revoke with: "
                      f"herdr-eng artifacts share revoke {_safe(aid)} {_safe(args.share_id)}")
        return 0
    except ShareError as error:
        message = _status_message(error)
        _err(message.replace(key, "[redacted]") if key else message)
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        message = str(error)
        _err(message.replace(key, "[redacted]") if key else message)
        return 1


def configure_parser(parser):
    commands = parser.add_subparsers(dest="share_action", required=True)
    for action in ("create", "show", "approve", "list", "events", "revoke"):
        c = commands.add_parser(action)
        c.add_argument("--control-plane")
        c.add_argument("--api-key-env", default="HERDR_API_KEY", help="Environment variable name, never the key itself")
        c.add_argument("artifact_id")
        if action == "create":
            c.add_argument("version_id")
            c.add_argument("--audience", choices=("lab", "public"), required=True)
            c.add_argument("--email", action="append", help="lab recipient email (repeatable)")
            c.add_argument("--group", action="append", help="lab recipient group (repeatable)")
            c.add_argument("--room", help="lab only: also allow the people in this room as they are now (not later joiners)")
            c.add_argument("--expires-in-days", type=int, help=f"1..{MAX_DAYS}; public links default to the server's 30")
            c.add_argument("--entry")
            c.add_argument("--title")
            c.add_argument("--check-pool", action="store_true", help="warn about emails without a Cognito account")
            c.add_argument("--pool-id")
            c.add_argument("--fleet-config", help="fleet config file to read control_plane.collab.cognito_pool from")
        elif action != "list":
            c.add_argument("share_id")
        if action == "approve":
            c.add_argument("--yes", action="store_true", help="confirm non-interactively after reviewing the summary")
        if action == "events":
            c.add_argument("--limit", type=int, help="show only the newest N events")
        if action in {"create", "show", "approve", "revoke"}:
            c.add_argument("--json", action="store_true", help="print the server's JSON only")
        c.set_defaults(func=run_command)
    return parser


def add_share_subparser(subparsers):
    return configure_parser(subparsers.add_parser("share", help="private, lab or public share links (approve-gated)"))


def main(argv=None):
    parser = configure_parser(argparse.ArgumentParser(prog="herdr-eng artifacts share"))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    return run_command(args)
