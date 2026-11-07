"""`herdr-eng rooms ...`: attach a team to a room, the guest level, the Suggest queue, roles, coordinators and the Direct level.

These are collaboration routes, so they authenticate as a PERSON: a collaboration bearer token read from the environment
variable named by ``--token-env`` (default ``HERDR_COLLAB_TOKEN``), never the legacy control-plane API key (which the
collaboration door ignores on purpose). Attach, detach, the guest level and the decisions are the room owner's; ``suggest``
and ``withdraw`` are a guest's. Every route answers 404 until ``control_plane.collab.team_rooms.enabled`` is true, and
``detach`` stays reachable either way. ``direct``, ``direct-tasks``, ``direct-cancel`` and ``direct-guests`` belong to the Direct
guest level and answer 404 until ``team_rooms.direct_enabled`` is true (withdrawing a guest's right does not). Human output passes server text through ``_safe``; a guest's request is printed as
quoted, untrusted text.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from .artifact_shares_client import ShareClient, ShareError, _safe

BASE = "/api/v1/collab"
TOKEN_ENV = "HERDR_COLLAB_TOKEN"
ACTIONS = (
    "attach",
    "detach",
    "teams",
    "guest-level",
    "queue",
    "approve",
    "reject",
    "suggest",
    "withdraw",
    # slice 2: roles and people as role holders (every route is 404 until team_roles.enabled is true)
    "roles",
    "assign-role",
    "clear-role",
    "role-policy",
    "member-visibility",
    "assign",
    "my-tasks",
    "respond",
    # slice 3: coordinators (every route is 404 until coordinators.enabled is true)
    "coordinator",
    "coordinator-clear",
    "external-coordination",
    "coordination",
    # the Direct guest level (every route is 404 until team_rooms.direct_enabled is true)
    "direct",
    "direct-tasks",
    "direct-cancel",
    "direct-guests",
    # slice 5: approval-gated external actions (every route is 404 until personal_assistant.external_actions.enabled is true)
    "issue-target",
    "external-list",
    "external-file",
    "external-decide",
    "external-retry",
)
EXTERNAL_ACTIONS = ("issue-target", "external-list", "external-file", "external-decide", "external-retry")


AGENT_MODE_ACTIONS = ("my-tasks", "respond")
_AGENT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_MACHINE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


def parse_self_machine(status_json):
    """This machine's MagicDNS name (``Self.DNSName`` without its trailing dot, lower-cased) from
    ``tailscale status --json`` output, or None. ``HostName`` is self-declared, so it is never used."""
    try:
        me = json.loads(status_json).get("Self") or {}
        name = str(me.get("DNSName") or "").rstrip(".").lower()
    except (ValueError, AttributeError):
        return None
    return name if _MACHINE.match(name) else None


def _tailscale_machine():
    """Best effort and silent: the header is informational (the control plane's own whois is authoritative)."""
    try:
        done = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_self_machine(done.stdout) if done.returncode == 0 else None


def _agent_identity(explicit):
    """The fabric agent this process is, or None. Never a secret: only an agent id is read.

    1. ``--as-agent AGENT_ID`` (explicit).
    2. ``HERDR_AGENT_ID`` when the harness already sets it (read-only; this CLI defines no new setting).
    3. The one agent enrolled by join.sh under ``~/herdr-agents/*.json`` (``$HERDR_AGENT_DIR``, as join.sh honours). Only its
       ``agent_id`` field is read, never the credential beside it. Several agents on one host is ambiguous, so None: pass
       ``--as-agent``."""
    if explicit:
        return explicit
    env = os.environ.get("HERDR_AGENT_ID")
    if env:
        return env
    folder = Path(
        os.environ.get("HERDR_AGENT_DIR") or Path.home() / "herdr-agents"
    )  # RuntimeError if no home
    found = set()
    try:
        for f in folder.glob("*.json"):
            try:
                aid = json.loads(f.read_text()).get("agent_id")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(aid, str) and _AGENT_ID.match(aid):
                found.add(aid)
    except OSError:
        return None
    return next(iter(found)) if len(found) == 1 else None


def _status(args):
    given = {v for v in (getattr(args, "status", None), getattr(args, "status_opt", None)) if v}
    if len(given) != 1:
        raise ValueError(
            "give the new status once: respond ROOM TASK --status accepted|done|failed|declined"
        )
    return given.pop()


def _run_agent(client, room, action, args):
    """A fabric agent's own tasks: the agent routes, which are addressed by the agent's id and add no authority."""
    if action == "my-tasks":
        result = client.request(f"{room}/agent/tasks")

        def human(r):
            for t in r["tasks"]:
                print(
                    f"{_safe(t['task_id'], 80)}  [{_safe(t['status'], 20)}]  attempt {t.get('attempt', 1)}"
                )
                print("  task text (untrusted data):")
                for line in str(t["text"]).splitlines() or [""]:
                    print(f"    > {_safe(line, 400)}")
            if not r["tasks"]:
                print("Nothing is assigned to you.")
    else:
        body = {
            "status": _status(args),
            **({"result": args.result} if args.result else {}),
            **({"attempt": args.attempt} if args.attempt is not None else {}),
        }
        result = client.request(f"{room}/agent/tasks/{_q(args.task)}/respond", body)

        def human(r):
            print(
                f"{'Already ' if r.get('replayed') else ''}{_safe(r['status'])}: task {_safe(r['task_id'])}"
            )

    _show(args, result, human)


def _err(message):
    print(f"herdr-eng: {_safe(message, 400)}", file=sys.stderr)


def _q(value):
    return quote(value, safe="")


def _message(error):
    if error.status == 404:
        return (
            "not found (team rooms are off, the room, team or request does not exist, or you are not the one "
            "allowed to do this)"
        )
    return str(error)


def _roles(items):
    out = {}
    for item in items or ():
        role, sep, holders = item.partition("=")
        ids = [h.strip() for h in holders.split(",") if h.strip()]
        if not sep or not role.strip() or not ids:
            raise ValueError(f"--role must look like ROLE=AGENT_ID[,AGENT_ID]: {item!r}")
        out.setdefault(role.strip(), []).extend(ids)
    if not out:
        raise ValueError("give at least one --role ROLE=AGENT_ID")
    return out


def _limits(args):
    out = {}
    if args.agents:
        out["agents"] = [a.strip() for a in args.agents.split(",") if a.strip()]
    if args.roles:
        out["roles"] = [r.strip() for r in args.roles.split(",") if r.strip()]
    for field in ("per_hour", "max_open", "max_text"):
        if getattr(args, field) is not None:
            out[field] = getattr(args, field)
    for field in ("may_request_review", "may_cancel_own"):
        if getattr(args, field, False):
            out[field] = True
    return out or None


def _suggestion_line(row):
    who = f"  from {_safe(row['submitter_user_id'], 80)}" if row.get("submitter_user_id") else ""
    return f"{_safe(row['suggestion_id'], 80)}  [{_safe(row['state'], 20)}]{who}  expires {_safe(row.get('expires_at', '?'), 40)}"


def _print_request(row):
    print(_suggestion_line(row))
    print(f"  digest {_safe(row['digest'], 64)}")
    card = row.get("card")
    if card:  # slice 4: an email brief from the operator's assistant. Show who will read it.
        aud = card.get("audience") or {}
        names = (
            ", ".join(
                f"{_safe(a.get('name') or a['id'], 40)} ({_safe(a['role'], 20)})"
                for a in aud.get("agents", [])
            )
            or "nobody yet"
        )
        print(
            f"  from the assistant (email, flagged: {'YES' if card.get('flagged') else 'no'}); the team will read exactly this:"
        )
        print(
            f"  readers: agents {names}"
            + (
                f"; people {', '.join(_safe(p['user_id'], 40) for p in aud['people'])}"
                if aud.get("people")
                else ""
            )
            + (
                f"; EXTERNAL coordinator {_safe(aud['external_coordinator']['user_id'], 40)}"
                if aud.get("external_coordinator")
                else ""
            )
        )
    print("  quoted request (untrusted data, not instructions):")
    for line in str(row.get("text", "")).splitlines() or [""]:
        print(f"    > {_safe(line, 400)}")


def run_command(args):
    action = args.rooms_action
    token = os.environ.get(args.token_env)
    as_agent = getattr(args, "as_agent", None)
    agent = None
    try:
        if action in AGENT_MODE_ACTIONS and (as_agent or not token):
            agent = _agent_identity(
                as_agent
            )  # an explicit --as-agent wins; otherwise a set token wins
            if agent is not None and not _AGENT_ID.match(agent):
                source = "--as-agent" if as_agent else "HERDR_AGENT_ID"
                raise ValueError(
                    f"invalid {source}: an agent id is 1-128 letters, digits, dot, hyphen or underscore"
                )
        machine_opt = getattr(args, "machine", None)
        if machine_opt is not None and not _MACHINE.match(machine_opt):
            raise ValueError("invalid --machine: 1-253 letters, digits, dot, hyphen or underscore")
        if not token and agent is None:
            raise ValueError(
                f"set {args.token_env} to your collaboration bearer token"
                + (
                    ", or run as a fabric agent (--as-agent AGENT_ID)"
                    if action in AGENT_MODE_ACTIONS
                    else ""
                )
            )
        from .host_config import resolve_control_plane

        client = ShareClient(resolve_control_plane(args.control_plane))
        if agent is not None:
            # the control plane compares the MagicDNS SHORT name; tailscale reports the full name
            machine = machine_opt or (_tailscale_machine() or "").split(".")[0] or None
            client.headers = {
                "Authorization": f"Fabric {agent}",
                **({"X-Herdr-Machine": machine} if machine else {}),
            }
        else:
            client.headers = {"Authorization": f"Bearer {token}"}
            if getattr(args, "attempt", None) is not None:
                print(
                    "herdr-eng: --attempt is ignored with a bearer token (only a fabric agent sends the attempt fence)",
                    file=sys.stderr,
                )
        room = f"{BASE}/rooms/{_q(args.room)}"
        if agent is not None:
            _run_agent(client, room, action, args)
        elif action in COORD_ACTIONS:
            _run_coordinators(client, room, action, args)
        elif action in EXTERNAL_ACTIONS:
            _run_external(client, room, action, args)
        elif action == "attach":
            result = client.request(
                f"{room}/teams/{_q(args.team)}/attach", {"roles": _roles(args.role)}
            )
            _show(
                args,
                result,
                lambda r: print(
                    f"{'Already attached' if r.get('replayed') else 'Attached'} {_safe(r['team_id'])}: "
                    + ", ".join(f"{_safe(a['role'])}={_safe(a['agent_id'])}" for a in r["agents"])
                ),
            )
        elif action == "detach":
            result = client.request(f"{room}/teams/{_q(args.team)}/detach", {})
            _show(
                args,
                result,
                lambda r: print(
                    ("Already detached " if r.get("replayed") else "Detached ")
                    + _safe(r["team_id"])
                    + (
                        ": removed " + ", ".join(_safe(a) for a in r["removed"])
                        if r["removed"]
                        else ""
                    )
                ),
            )
        elif action == "teams":
            result = client.request(f"{room}/teams")
            _show(
                args,
                result,
                lambda r: (
                    [
                        print(
                            f"{_safe(t['team_id'])}: "
                            + ", ".join(
                                f"{_safe(a['role'])}={_safe(a['agent_id'])}" for a in t["agents"]
                            )
                        )
                        for t in r["teams"]
                    ]
                    or print("No teams attached.")
                ),
            )
        elif action == "guest-level":
            if args.level is None:
                result = client.request(f"{room}/guest-level")
            else:
                body = {"level": args.level}
                limits = _limits(args)
                if limits:
                    body["direct_limits"] = limits
                result = client.request(f"{room}/guest-level", body)
            _show(
                args,
                result,
                lambda r: print(
                    f"Guest level: {_safe(r['level'])}"
                    + (
                        f" (acts as {_safe(r['behaves_as'])}: direct is switched off on this service)"
                        if r["level"] != r["behaves_as"]
                        else ""
                    )
                    + ("" if r.get("explicit") else " (default)")
                    + (
                        f"  limits {_safe(json.dumps(r['effective_limits']), 300)}"
                        if r.get("effective_limits")
                        else ""
                    )
                ),
            )
        elif action == "queue":
            query = urlencode({"state": args.state}) if args.state else ""
            result = client.request(f"{room}/suggestions" + ("?" + query if query else ""))

            def human(r):
                for row in r["suggestions"]:
                    _print_request(row)
                if not r["suggestions"]:
                    print("No requests.")

            _show(args, result, human)
        elif action == "approve":
            row = client.request(f"{room}/suggestions/{_q(args.suggestion)}")
            if not args.json:
                _print_request(row)
            body = {
                "decision": "approve",
                "expected_digest": args.digest or row["digest"],
                "target_agent_id": args.agent,
            }
            for field, key in (
                ("command", "command"),
                ("edited_text", "edited_text"),
                ("reason", "reason"),
            ):
                if getattr(args, field):
                    body[key] = getattr(args, field)
            result = client.request(f"{room}/suggestions/{_q(args.suggestion)}/decide", body)
            _show(
                args,
                result,
                lambda r: print(
                    f"{'Already approved' if r.get('replayed') else 'Approved'}: task {_safe(r.get('task_id'))} "
                    f"created for {_safe(args.agent)} ({_safe(r['state'])})"
                ),
            )
        elif action == "reject":
            row = client.request(f"{room}/suggestions/{_q(args.suggestion)}")
            body = {"decision": "reject", "expected_digest": args.digest or row["digest"]}
            if args.reason:
                body["reason"] = args.reason
            result = client.request(f"{room}/suggestions/{_q(args.suggestion)}/decide", body)
            _show(
                args,
                result,
                lambda r: print(
                    f"{'Already rejected' if r.get('replayed') else 'Rejected'}: {_safe(r['suggestion_id'])}"
                ),
            )
        elif action == "suggest":
            body = {"text": args.text}
            if args.key:
                body["idempotency_key"] = args.key
            result = client.request(f"{room}/suggestions", body)
            _show(
                args,
                result,
                lambda r: print(
                    f"{'Already filed' if r.get('replayed') else 'Filed'}: {_safe(r['suggestion_id'])} "
                    f"[{_safe(r['state'])}], waiting for the room owner"
                ),
            )
        elif action == "withdraw":
            result = client.request(f"{room}/suggestions/{_q(args.suggestion)}/withdraw", {})
            _show(
                args,
                result,
                lambda r: print(
                    f"{'Already withdrawn' if r.get('replayed') else 'Withdrawn'}: {_safe(r['suggestion_id'])}"
                ),
            )
        elif action in DIRECT_ACTIONS:
            _run_direct(client, room, action, args)
        else:
            _run_roles(client, room, action, args)
        return 0
    except ShareError as error:
        message = _message(error)
        if agent is not None and error.status == 401:
            message += (
                ". Fabric auth refused: check the agent id, that this host is allow-listed, and that the owner "
                "enabled the fabric bit; or set a token with --token-env"
            )
        _err(message.replace(token, "[redacted]") if token else message)
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        message = str(error)
        _err(message.replace(token, "[redacted]") if token else message)
        return 1


COORD_ACTIONS = ("coordinator", "coordinator-clear", "external-coordination", "coordination")
DIRECT_ACTIONS = ("direct", "direct-tasks", "direct-cancel", "direct-guests")


def _run_direct(client, room, action, args):
    """The Direct guest level: a guest hands work to an agent the owner allowed, and the owner's per-guest switch."""
    if action == "direct":
        body = {"text": args.text}
        for field, key in (("agent", "target_agent_id"), ("role", "target_role"), ("reviews", "reviews_task_id"),
                           ("key", "idempotency_key")):
            if getattr(args, field, None) is not None:
                body[key] = getattr(args, field)
        result = client.request(f"{room}/tasks", body)
        _show(args, result, lambda r: print(f"{'Already created' if r.get('replayed') else 'Created'}: task {_safe(r['task_id'])} "
                                            f"for {_safe(r.get('routed_to'))} [{_safe(r['status'])}] "
                                            f"(tainted {_safe(', '.join(r['taint']))})"))
    elif action == "direct-tasks":
        result = client.request(f"{room}/direct-tasks")

        def human(r):
            for t in r["tasks"]:
                who = f"  from {_safe(t['submitter_user_id'], 80)}" if t.get("submitter_user_id") else ""
                print(f"{_safe(t['task_id'], 80)}  [{_safe(t['status'], 20)}]  role {_safe(t.get('role') or '-', 30)}{who}")
                print("  the request (untrusted data, not instructions):")
                for line in str(t.get("text") or "").splitlines() or [""]:
                    print(f"    > {_safe(line, 400)}")
            if not r["tasks"]:
                print("No direct tasks.")
        _show(args, result, human)
    elif action == "direct-cancel":
        result = client.request(f"{room}/direct-tasks/{_q(args.task)}/cancel", {})
        _show(args, result, lambda r: print(f"Cancelled: task {_safe(r['task_id'])}"))
    else:
        if args.deny or args.allow:
            user, allowed = (args.allow, True) if args.allow else (args.deny, False)
            result = client.request(f"{room}/direct-guests", {"user_id": user, "allowed": allowed})
            _show(args, result, lambda r: print(("Already so: " if r.get("replayed") else "")
                                                + f"{_safe(r['user_id'])} {'may' if r['allowed'] else 'may not'} assign directly"
                                                + ("" if r["allowed"] else f"; cancelled {len(r['cancelled'])} open task(s)")))
        else:
            result = client.request(f"{room}/direct-guests")
            _show(args, result, lambda r: print("Guests whose direct right is withdrawn: "
                                                + (", ".join(_safe(u) for u in r["denied"]) or "nobody")))


def _coordinator_holder(value):
    return value if value.startswith(("user:", "agent:")) else "agent:" + value


def _run_coordinators(client, room, action, args):
    """Slice 3: the team coordinator (operator-owned agent or opted-in external person) and the coordinator's own tracking."""
    team = f"{room}/teams/{_q(getattr(args, 'team', '') or '')}"

    def row(r):
        who = r.get("holder")
        print(
            f"Team {_safe(r['team_id'])}  coordinator: {_safe(who) if who else 'nobody'}"
            + (f" ({_safe(r['kind'])}{'' if r.get('active') else ', inactive'})" if who else "")
        )
        ext = r["external"]
        print(
            "  external coordination: "
            + ("on" if ext["enabled"] else "off")
            + ("" if ext["service_enabled"] else " (switched off for the whole service)")
            + (
                f"  roles {', '.join(_safe(x) for x in ext['limits']['roles'])}"
                if ext.get("limits")
                else ""
            )
        )
        if who:
            print(
                f"  limits {_safe(json.dumps(r['limits']), 200)}  usage {_safe(json.dumps(r['usage']), 100)}"
            )

    if action == "coordinator":
        if args.holder is not None:
            client.request(f"{team}/coordinator", {"holder": _coordinator_holder(args.holder)})
        _show(args, client.request(f"{team}/coordinator"), row)
    elif action == "coordinator-clear":
        result = client.request(f"{team}/coordinator/clear", {})
        _show(
            args,
            result,
            lambda r: print(
                ("Already clear" if r.get("replayed") else "Cleared")
                + f": cancelled {len(r.get('cancelled', []))}, requeued {len(r.get('requeued', []))}"
            ),
        )
    elif action == "external-coordination":
        if args.state is None:
            result = {"team_id": args.team, **client.request(f"{team}/coordinator")["external"]}
            _show(
                args,
                result,
                lambda r: print(
                    "External coordination: "
                    + ("on" if r["enabled"] else "off")
                    + (
                        f"  roles {', '.join(_safe(x) for x in r['limits']['roles'])}"
                        if r.get("limits")
                        else ""
                    )
                ),
            )
        else:
            body = {"enabled": args.state == "on"}
            if args.state == "on":
                limits = {"roles": [r.strip() for r in (args.roles or "").split(",") if r.strip()]}
                for field, key in (
                    ("per_hour", "tasks_per_hour"),
                    ("max_open", "max_open"),
                    ("max_depth", "max_depth"),
                    ("max_items", "max_items_per_request"),
                ):
                    if getattr(args, field) is not None:
                        limits[key] = getattr(args, field)
                body["limits"] = limits
            result = client.request(f"{team}/external-coordination", body)
            _show(
                args,
                result,
                lambda r: print(
                    ("Already " if r.get("replayed") else "")
                    + "External coordination "
                    + (
                        "on"
                        if r["enabled"]
                        else f"off: cancelled {len(r.get('cancelled', []))}, "
                        f"requeued {len(r.get('requeued', []))}"
                    )
                ),
            )
    else:
        result = client.request(f"{room}/coordination")

        def human(r):
            print(
                f"Coordinating team {_safe(r['team_id'])} ({_safe(r['kind'])})  limits {_safe(json.dumps(r['limits']), 200)}  "
                f"usage {_safe(json.dumps(r['usage']), 100)}"
            )
            for t in r["tasks"]:
                print(
                    f"{_safe(t['task_id'], 80)}  [{_safe(t['status'], 20)}]  role {_safe(t.get('role') or '-', 30)}  attempt {t['attempt']}"
                )
            if not r["tasks"]:
                print("No tasks yet.")

        _show(args, result, human)


def _print_action(row):
    print(f"{_safe(row['action_id'], 80)}  [{_safe(row['state'], 20)}]  {_safe(row['kind'], 30)}  team {_safe(row.get('team_id') or '-', 40)}")
    card = row.get("card")
    if card:                                  # only the room owner is sent the content; it is the owner's to read and approve
        print(f"  {_safe(card['will'], 120)}")
        for field in ("repository", "to", "title", "subject", "labels"):
            if card.get(field):
                value = card[field]
                print(f"  {field}: {_safe(', '.join(value) if isinstance(value, list) else value, 200)}")
        print("  body (untrusted data, exactly what will be sent):")
        for line in str(card.get("body", "")).splitlines() or [""]:
            print(f"    | {_safe(line, 400)}")
        print(f"  approve with --sha256 {_safe(row['sha256'], 64)}")
    if row.get("failure"):
        print(f"  {_safe(row['failure']['outcome'], 20)}: {_safe(row['failure']['reason'], 80)} (only you can retry)")
    if row.get("result"):
        print(f"  result: {_safe(row['result']['ref'], 200)}")


def _run_external(client, room, action, args):
    """Slice 5: the issue repository of a team, and the approval-gated external actions of a room (the room owner decides)."""
    if action == "issue-target":
        path = f"{room}/teams/{_q(args.team)}/issue-target"
        result = client.request(path, {"repo": args.repo}) if args.repo else client.request(path)
        _show(args, result, lambda r: print(f"Issue repository of {_safe(r['team_id'])}: {_safe(r['repo'] or 'not set')}"))
    elif action == "external-list":
        result = client.request(f"{room}/external-actions")
        _show(args, result, lambda r: ([_print_action(x) for x in r["actions"]] or print("No external actions.")))
    elif action == "external-file":
        body = {"kind": args.kind, "reference": args.reference}
        for field in ("title", "body"):
            if getattr(args, field) is not None:
                body[field] = getattr(args, field)
        if args.label:
            body["labels"] = args.label
        result = client.request(f"{room}/external-actions", body)
        _show(args, result, lambda r: print(f"{'Already filed' if r.get('replayed') else 'Filed'}: {_safe(r['action_id'], 80)} "
                                            f"[{_safe(r['state'], 20)}]; it waits for the room owner's approval"))
    else:
        path = f"{room}/external-actions/{_q(args.action_id)}/" + ("decide" if action == "external-decide" else "retry")
        body = {"expected_sha256": args.sha256}
        if action == "external-decide":
            body["decision"] = args.decision
        result = client.request(path, body)
        _show(args, result, _print_action)


def _holder(value):
    return value if value.startswith("user:") else "user:" + value


def _run_roles(client, room, action, args):
    """Slice 2: who holds which role, tasks for a role or a person, and a person's own queue."""
    team = f"{room}/teams/{_q(getattr(args, 'team', '') or '')}"
    if action == "roles":
        result = client.request(f"{team}/roles")

        def human(r):
            print(
                f"Team {_safe(r['team_id'])}  no-response policy: {_safe(r['no_response_policy'])}"
            )
            for role in r["roles"]:
                held = ", ".join(
                    f"{_safe(h['holder'])}{'' if h['can_take_tasks'] else ' (cannot take tasks)'}"
                    for h in role["holders"]
                )
                print(f"  {_safe(role['role'])} ({_safe(role['kind'])}): {held or 'nobody'}")

        _show(args, result, human)
    elif action in ("assign-role", "clear-role"):
        path = f"{team}/roles" + ("/clear" if action == "clear-role" else "")
        result = client.request(path, {"role": args.role_name, "holder": _holder(args.holder)})
        verb = "Cleared" if action == "clear-role" else "Assigned"
        _show(
            args,
            result,
            lambda r: print(
                f"{'Already so: ' if r.get('replayed') else verb + ' '}{_safe(r['holder'])} as {_safe(r['role'])}"
            ),
        )
    elif action == "role-policy":
        if args.policy is None:
            current = client.request(f"{team}/roles")
            result = {"no_response_policy": current["no_response_policy"], "team_id": args.team}
        else:
            result = client.request(f"{team}/policy", {"no_response_policy": args.policy})
        _show(
            args,
            result,
            lambda r: print(
                f"No-response policy for {_safe(r['team_id'])}: {_safe(r['no_response_policy'])}"
            ),
        )
    elif action == "member-visibility":
        if args.state is None:
            result = client.request(f"{room}/member-visibility")
        else:
            result = client.request(f"{room}/member-visibility", {"enabled": args.state == "on"})
        _show(
            args,
            result,
            lambda r: print("Members see the task list: " + ("on" if r["enabled"] else "off")),
        )
    elif action == "assign":
        body = {"text": args.text}
        for field, key in (
            ("role", "target_role"),
            ("team", "team_id"),
            ("user", "target_user_id"),
            ("lease", "lease_seconds"),
            ("policy", "no_response_policy"),
            ("reviews", "reviews_task_id"),
            ("key", "idempotency_key"),
        ):
            if getattr(args, field, None) is not None:
                body[key] = getattr(args, field)
        result = client.request(f"{room}/tasks", body)
        _show(
            args,
            result,
            lambda r: print(
                f"{'Already created' if r.get('replayed') else 'Created'}: task {_safe(r['task_id'])} "
                f"for {_safe(r.get('routed_to'))}"
            ),
        )
    elif action == "my-tasks":
        result = client.request(f"{room}/my-tasks")

        def human(r):
            for t in r["tasks"]:
                print(
                    f"{_safe(t['task_id'], 80)}  [{_safe(t['status'], 20)}]  role {_safe(t.get('role') or '-', 30)}  "
                    f"answer by {_safe(t['lease']['expires_at'], 40)}"
                )
                print("  task text (untrusted data):")
                for line in str(t["text"]).splitlines() or [""]:
                    print(f"    > {_safe(line, 400)}")
            if not r["tasks"]:
                print("Nothing is assigned to you.")

        _show(args, result, human)
    else:
        body = {"status": _status(args), **({"result": args.result} if args.result else {})}
        result = client.request(f"{room}/tasks/{_q(args.task)}/respond", body)
        _show(
            args,
            result,
            lambda r: print(
                f"{'Already ' if r.get('replayed') else ''}{_safe(r['status'])}: task {_safe(r['task_id'])}"
            ),
        )


def _show(args, result, human):
    if args.json:
        print(json.dumps(result))
    else:
        human(result)


def configure_parser(parser):
    commands = parser.add_subparsers(dest="rooms_action", required=True)
    for action in ACTIONS:
        c = commands.add_parser(action)
        c.add_argument("--control-plane")
        c.add_argument(
            "--token-env",
            default=TOKEN_ENV,
            help="Environment variable NAME holding your collaboration bearer token, never the token",
        )
        c.add_argument("--json", action="store_true", help="print the server's JSON only")
        c.add_argument("room")
        if action in ("attach", "detach", "roles", "assign-role", "clear-role", "role-policy"):
            c.add_argument("team", help="the team id (herdr-eng teams list)")
        if action in ("assign-role", "clear-role"):
            c.add_argument(
                "role_name",
                metavar="role",
                choices=("issue_writer", "implementer", "tester", "reviewer"),
            )
            c.add_argument(
                "holder",
                help="the person: user:ID (or just the ID). Agents join a role through attach",
            )
        if action in ("coordinator", "coordinator-clear", "external-coordination", "issue-target"):
            c.add_argument("team", help="the team id (herdr-eng teams list)")
        if action == "issue-target":
            c.add_argument("repo", nargs="?", help="OWNER/NAME; omit to show. Owner only")
        if action == "external-file":
            c.add_argument("kind", choices=("github_issue_create", "email_reply_draft"))
            c.add_argument("reference", help="the approved email brief's reference (bref-...)")
            c.add_argument("--title", help="issue only")
            c.add_argument("--label", action="append", help="issue only; one of the operator's allowed labels; repeatable")
            c.add_argument("--body", help="draft only: the reply text (plain, up to 800 characters)")
        if action in ("external-decide", "external-retry"):
            c.add_argument("action_id")
            c.add_argument("--sha256", required=True, help="the sha256 of the content you were shown (external-list prints it)")
        if action == "external-decide":
            c.add_argument("decision", choices=("approved", "rejected"))
        if action == "direct":
            c.add_argument("text", help="what you want done; it reaches the agent as quoted, untrusted data")
            c.add_argument("--agent", help="the agent id (it must be one the room owner allowed)")
            c.add_argument("--role", choices=("issue_writer", "implementer", "tester", "reviewer"), help="route to the role's holder")
            c.add_argument("--reviews", help="reviewer role only: the direct task you want reviewed (needs may_request_review)")
            c.add_argument("--key", help="idempotency key")
        if action == "direct-cancel":
            c.add_argument("task")
        if action == "direct-guests":
            c.add_argument("--deny", metavar="USER", help="withdraw this guest's direct right now and cancel their open tasks")
            c.add_argument("--allow", metavar="USER", help="give this guest's direct right back")
        if action == "coordinator":
            c.add_argument(
                "holder",
                nargs="?",
                help="agent:ID (your own agent) or user:ID (an external person, after the opt-in); omit to show",
            )
        if action == "external-coordination":
            c.add_argument(
                "state",
                nargs="?",
                choices=("on", "off"),
                help="omit to show; off revokes at once and requeues their open work",
            )
            c.add_argument(
                "--roles",
                help="on only: comma-separated team roles the person may assign to (required)",
            )
            c.add_argument("--per-hour", dest="per_hour", type=int)
            c.add_argument("--max-open", dest="max_open", type=int)
            c.add_argument("--max-depth", dest="max_depth", type=int)
            c.add_argument("--max-items", dest="max_items", type=int)
        if action == "role-policy":
            c.add_argument(
                "policy",
                nargs="?",
                choices=("reassign", "escalate", "wait"),
                help="omit to show the current policy",
            )
        if action == "member-visibility":
            c.add_argument(
                "state",
                nargs="?",
                choices=("on", "off"),
                help="omit to show; on lets members see the room's task list",
            )
        if action == "assign":
            c.add_argument("text")
            c.add_argument(
                "--role",
                choices=("issue_writer", "implementer", "tester", "reviewer"),
                help="route to the role's holder",
            )
            c.add_argument(
                "--team", help="the team, when more than one attached team holds the role"
            )
            c.add_argument(
                "--user", help="name a person directly (a room member) instead of a role"
            )
            c.add_argument(
                "--lease", type=int, help="seconds the person has to answer (room owner only)"
            )
            c.add_argument(
                "--policy",
                choices=("reassign", "escalate", "wait"),
                help="what happens with no answer (room owner only)",
            )
            c.add_argument(
                "--reviews", help="the task this one reviews (a reviewer must be independent of it)"
            )
            c.add_argument("--key", help="idempotency key")
        if action == "respond":
            c.add_argument("task")
            c.add_argument(
                "status",
                nargs="?",
                choices=("accepted", "done", "declined", "failed"),
                help="accepted, done or declined (failed: a fabric agent only); or use --status",
            )
            c.add_argument(
                "--status", dest="status_opt", choices=("accepted", "done", "declined", "failed")
            )
            c.add_argument("--result")
            c.add_argument(
                "--attempt",
                type=int,
                help="agent mode: the attempt number from my-tasks (the lease fence)",
            )
        if action in AGENT_MODE_ACTIONS:
            c.add_argument(
                "--as-agent",
                dest="as_agent",
                metavar="AGENT_ID",
                help="act as this fabric agent (no token): Authorization: Fabric AGENT_ID, vouched for by the "
                "tailnet machine. Without a token the agent is also found from HERDR_AGENT_ID or the one "
                "agent in ~/herdr-agents",
            )
            c.add_argument(
                "--machine",
                help="agent mode: this machine's MagicDNS short name (default: asked of tailscale)",
            )
        if action == "attach":
            c.add_argument(
                "--role",
                action="append",
                metavar="ROLE=AGENT_ID[,AGENT_ID]",
                help="issue_writer, implementer, tester or reviewer; repeat for each role. Only your own agents",
            )
        elif action == "guest-level":
            c.add_argument(
                "level",
                nargs="?",
                choices=("read_only", "suggest", "direct"),
                help="omit to show the current level. direct acts as suggest unless the service switched it on",
            )
            c.add_argument(
                "--agents", help="direct only: comma-separated agent ids a guest may address"
            )
            c.add_argument(
                "--roles", help="direct only: comma-separated team roles a guest may address"
            )
            c.add_argument("--per-hour", dest="per_hour", type=int)
            c.add_argument("--max-open", dest="max_open", type=int)
            c.add_argument("--max-text", dest="max_text", type=int)
            c.add_argument("--may-request-review", dest="may_request_review", action="store_true",
                           help="direct only: a guest may ask for a review (by role) of work they assigned")
            c.add_argument("--may-cancel-own", dest="may_cancel_own", action="store_true",
                           help="direct only: a guest may cancel their own open direct tasks")
        elif action == "queue":
            c.add_argument(
                "--state",
                choices=("pending", "approved", "materialized", "rejected", "withdrawn", "expired"),
            )
        elif action in ("approve", "reject", "withdraw"):
            c.add_argument("suggestion")
            if action != "withdraw":
                c.add_argument(
                    "--digest",
                    help="the digest of the request you were shown (default: the one just fetched)",
                )
                c.add_argument("--reason")
            if action == "approve":
                c.add_argument(
                    "--agent",
                    required=True,
                    help="the agent that will do the work (it must be able to execute in this room)",
                )
                c.add_argument(
                    "--command",
                    help="the task text; default is a fixed sentence. The request itself rides as quoted data",
                )
                c.add_argument(
                    "--edited-text",
                    dest="edited_text",
                    help="trim the quoted request before it is attached",
                )
        elif action == "suggest":
            c.add_argument("text")
            c.add_argument("--key", help="idempotency key: the same key and text is one request")
        c.set_defaults(func=run_command)
    return parser


def add_rooms_subparser(subparsers):
    return configure_parser(
        subparsers.add_parser(
            "rooms",
            help="team rooms: attach a team, the guest level, and the Suggest queue (collaboration token)",
        )
    )


def main(argv=None):
    parser = configure_parser(argparse.ArgumentParser(prog="herdr-eng rooms"))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    return run_command(args)
