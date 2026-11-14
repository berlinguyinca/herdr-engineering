"""`herdr-eng collab agents list|renew`: the operator's own collaboration agents.

Talks to the collab OPERATOR listener (HERDR_COLLAB_OPERATOR_URL, default http://127.0.0.1:9482): it acts as the
signed-in operator and takes no token, so this client sends no credential of its own. Renewing returns a NEW agent
credential exactly once. It is written only to the file named by --credential-file (created 0600, an existing file is
refused without --force) or, when --print-credential is given, to stdout. It is never logged or put in --json output, except that `--json --print-credential` carries it as the `credential` key of the one JSON object.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

DEFAULT_OPERATOR_URL = "http://127.0.0.1:9482"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
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


def _check_url(base):
    """http(s) only; plain http only to loopback, because the renew answer carries the new credential."""
    try:
        u = urlparse(base)
        host = u.hostname
    except ValueError:
        raise AgentsError(f"--operator-url must be an http(s) URL, got {base!r}") from None
    if u.scheme not in ("http", "https") or not host:
        raise AgentsError(f"--operator-url must be an http(s) URL, got {base!r}")
    if u.scheme == "http" and host not in LOOPBACK:
        raise AgentsError("--operator-url: plain http is only allowed to loopback (the answer carries a credential); use https")


def _call(base, method, path, timeout=15.0):
    _check_url(base)
    req = urllib.request.Request(base.rstrip("/") + BASE + path, method=method,
                                 data=b"{}" if method == "POST" else None,
                                 headers={"Content-Type": "application/json"} if method == "POST" else {})
    unknown = ("outcome unknown; the agent may have been renewed: re-run `collab agents renew` (or check `collab agents list`)"
               if method == "POST" else "collab operator listener answered with something unreadable")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read(4_000_000) or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(4096)).get("error")
        except (ValueError, AttributeError, OSError):      # unreadable body, or the read timed out
            detail = None
        text = detail if isinstance(detail, str) else HINTS.get(exc.code, "request failed")
        raise AgentsError(f"HTTP {exc.code}: {text[:300]}") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):        # nothing was sent: the request cannot have taken effect
            raise AgentsError("collab operator listener is unreachable") from None
        raise AgentsError(unknown if method == "POST" else "collab operator listener is unreachable") from None
    except (OSError, ValueError):                                 # timeout, reset or bad body after the request went out
        raise AgentsError(unknown) from None
    if not isinstance(body, dict):
        raise AgentsError(unknown)
    return body


def _agents(base):
    agents = _call(base, "GET", "/agents").get("agents")
    if not isinstance(agents, list) or not all(isinstance(a, dict) for a in agents):
        raise AgentsError("collab operator listener answered with a malformed agent list")
    return agents


def _resolve(base, ref):
    """An agent id, or @name -> the id of the operator's one non-revoked agent of that name."""
    if not ref.startswith("@"):
        return ref
    rows = [a for a in _agents(base) if a.get("name") == ref[1:] and a.get("status") != "revoked"]
    if not rows:
        raise AgentsError(f"no agent named {ref[1:]!r}")
    if len(rows) > 1:
        raise AgentsError(f"{len(rows)} agents are named {ref[1:]!r}; use the agent id")
    agent_id = rows[0].get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise AgentsError(f"the agent named {ref[1:]!r} has no agent id in the listener's answer")
    return agent_id


def _reserve(path, force):
    """Create the 0600 temp file beside `path` before renewing, so an unwritable or refused target never costs the old credential."""
    if os.path.isdir(path):
        raise AgentsError(f"{path} is a directory; give a file path")
    if os.path.lexists(path) and not force:
        raise AgentsError(f"{path} exists; pass --force to overwrite it")
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        return tmp, os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise AgentsError(f"cannot write {path}: {exc.strerror}") from None


def _discard(tmp, fd):
    if fd is not None:
        os.close(fd)
    if tmp is not None:
        os.unlink(tmp)


def _store(credential, tmp, fd, dest, to_stdout=False):
    """Write the new credential to the reserved temp file and move it into place. The old credential is already dead, so a
    failure here must not lose the new one: keep the temp file (path to stderr) or, with no file at all, put it on stderr
    (on stdout instead when the caller asked for it there with --print-credential)."""
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(credential + "\n")
            fh.flush()
            os.fsync(fh.fileno())                                 # durable before the rename makes it the live file
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if to_stdout:
            print(f"collab agents: the agent WAS renewed but writing {dest} failed ({exc.strerror}). This is the only copy of "
                  "the new credential; it is printed on stdout as requested: save it now.", file=sys.stderr)
            print(credential)
        else:
            print(f"collab agents: the agent WAS renewed but writing {dest} failed ({exc.strerror}). This is the only copy of "
                  f"the new credential; save it now: {credential}", file=sys.stderr)
        return False
    try:
        os.replace(tmp, dest)
    except OSError as exc:
        print(f"collab agents: the agent WAS renewed and the credential is saved in {tmp} (0600) because moving it to {dest} "
              f"failed ({exc.strerror}). Move it into place yourself; re-running renew would replace it.", file=sys.stderr)
        return False
    return True


def _cmd_list(args):
    rows = [{k: a.get(k) for k in FIELDS} for a in _agents(args.operator_url)]
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
    except BaseException:
        _discard(tmp, fd)
        raise
    credential = out.pop("credential", None)
    if not isinstance(credential, str) or not credential:
        _discard(tmp, fd)
        raise AgentsError("the control plane answered without a credential; outcome unknown, the agent may have been renewed: re-run renew")
    if args.credential_file and not _store(credential, tmp, fd, args.credential_file, args.print_credential):
        return 1
    shown = {k: out.get(k) for k in ("agent_id", "expires_at", "grants_restored")}
    if args.json:
        print(json.dumps({**shown, "credential": credential} if args.print_credential else shown))
    else:
        print(f"renewed agent {shown['agent_id']}: new expires_at={shown['expires_at']}, "
              f"grants_restored={json.dumps(shown['grants_restored'])}")
        if args.credential_file:
            print(f"credential written to {args.credential_file} (0600); update the harness config with it")
    if args.print_credential and not args.json:
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
                               ("renew", "renew an agent; the NEW credential goes to a 0600 file", _cmd_renew)):
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
