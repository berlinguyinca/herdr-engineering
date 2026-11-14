"""`herdr-eng collab agents list|renew`: the operator's own collaboration agents.

Talks to the collab OPERATOR listener (HERDR_COLLAB_OPERATOR_URL, default http://127.0.0.1:9482): it acts as the
signed-in operator and takes no token, so this client sends no credential of its own. Renewing returns a NEW agent
credential exactly once. It is written only to the file named by --credential-file (created 0600, an existing file is
refused without --force) or, when --print-credential is given, to stdout. It is never logged or put in --json output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

DEFAULT_OPERATOR_URL = "http://127.0.0.1:9482"
BASE = "/api/v1/collab"
FIELDS = ("agent_id", "name", "harness_type", "status", "expires_at", "renewable")
HINTS = {404: "not found (only the owner can renew an agent)", 403: "this agent cannot be renewed",
         409: "conflict", 429: "too many agents or requests; try again later"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class AgentsError(RuntimeError):
    pass


def _call(base, method, path, timeout=15.0):
    req = urllib.request.Request(base.rstrip("/") + BASE + path, method=method,
                                 data=b"{}" if method == "POST" else None,
                                 headers={"Content-Type": "application/json"} if method == "POST" else {})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read(4_000_000) or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(4096)).get("error")
        except (ValueError, AttributeError):
            detail = None
        text = detail if isinstance(detail, str) else HINTS.get(exc.code, "request failed")
        raise AgentsError(f"HTTP {exc.code}: {text[:300]}") from None
    except (OSError, ValueError):
        raise AgentsError("collab operator listener is unreachable") from None


def _resolve(base, ref):
    """An agent id, or @name -> the id of the operator's one non-revoked agent of that name."""
    if not ref.startswith("@"):
        return ref
    rows = [a for a in _call(base, "GET", "/agents")["agents"] if a["name"] == ref[1:] and a["status"] != "revoked"]
    if not rows:
        raise AgentsError(f"no agent named {ref[1:]!r}")
    if len(rows) > 1:
        raise AgentsError(f"{len(rows)} agents are named {ref[1:]!r}; use the agent id")
    return rows[0]["agent_id"]


def _reserve(path, force):
    """Create the 0600 temp file beside `path` before renewing, so an unwritable or refused target never costs the old credential."""
    if os.path.lexists(path) and not force:
        raise AgentsError(f"{path} exists; pass --force to overwrite it")
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        return tmp, os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise AgentsError(f"cannot write {path}: {exc.strerror}") from None


def _cmd_list(args):
    rows = [{k: a.get(k) for k in FIELDS} for a in _call(args.operator_url, "GET", "/agents")["agents"]]
    if args.json:
        print(json.dumps(rows))
        return 0
    for r in rows:
        print(f"{r['agent_id']}  {r['name']}  {r['harness_type']}  {r['status']}  "
              f"expires_at={r['expires_at']}  renewable={'yes' if r['renewable'] else 'no'}")
    return 0


def _cmd_renew(args):
    if not (args.credential_file or args.print_credential):
        print("renew: pass --credential-file PATH (or --print-credential): the old credential stops working at once "
              "and the new one is shown only once", file=sys.stderr)
        return 2
    tmp = fd = None
    if args.credential_file:
        tmp, fd = _reserve(args.credential_file, args.force)
    try:
        agent_id = _resolve(args.operator_url, args.agent)
        out = _call(args.operator_url, "POST", f"/agents/{quote(agent_id, safe='')}/renew")
        credential = out.pop("credential")
        if fd is not None:
            with os.fdopen(fd, "w") as fh:
                fd = None
                fh.write(credential + "\n")
            os.replace(tmp, args.credential_file)
            tmp = None
    except (KeyError, TypeError):
        raise AgentsError("the control plane answered without a credential") from None
    finally:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            os.unlink(tmp)
    shown = {k: out.get(k) for k in ("agent_id", "expires_at", "grants_restored")}
    if args.json:
        print(json.dumps(shown))
    else:
        print(f"renewed agent {shown['agent_id']}: new expires_at={shown['expires_at']}, "
              f"grants_restored={json.dumps(shown['grants_restored'])}")
        if args.credential_file:
            print(f"credential written to {args.credential_file} (0600); update the harness config with it")
    if args.print_credential:
        print(credential)
    return 0


def run_command(args):
    try:
        return args.agents_func(args)
    except AgentsError as exc:
        print(f"collab agents: {exc}", file=sys.stderr)
        return 1


def add_collab_subparser(sub):
    collab = sub.add_parser("collab", help="collaboration network helpers (operator listener)")
    csub = collab.add_subparsers(dest="collab_command", required=True)
    agents = csub.add_parser("agents", help="list and renew your collab agents")
    asub = agents.add_subparsers(dest="agents_command", required=True)
    for name, helptext, fn in (("list", "agents with expiry and renewable flag (never credentials)", _cmd_list),
                               ("renew", "renew an expired agent; the NEW credential goes to a 0600 file", _cmd_renew)):
        p = asub.add_parser(name, help=helptext)
        p.add_argument("--operator-url", default=os.environ.get("HERDR_COLLAB_OPERATOR_URL", DEFAULT_OPERATOR_URL),
                       help=f"collab operator listener (env HERDR_COLLAB_OPERATOR_URL, default {DEFAULT_OPERATOR_URL})")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=run_command, agents_func=fn)
        if name == "renew":
            p.add_argument("agent", help="agent id, or @name")
            p.add_argument("--credential-file", help="write the new credential here (created 0600)")
            p.add_argument("--force", action="store_true", help="overwrite an existing --credential-file")
            p.add_argument("--print-credential", action="store_true", help="print the new credential to stdout (explicit opt-in)")
    return collab


def main(argv=None):
    parser = argparse.ArgumentParser(prog="herdr-eng")
    add_collab_subparser(parser.add_subparsers(dest="command", required=True))
    try:
        args = parser.parse_args(["collab", "agents", *(argv or [])])
    except SystemExit as exit_:
        return int(exit_.code or 0)
    return args.func(args)
