#!/usr/bin/env python3
"""Add the people the operator invites to the Cognito group that gates collaboration.

Runs on the HOST from collab-sync.sh (once a minute) because the public collab container has
no AWS credentials, by design. It reads the container's append-only event log and, for every
still-open invitation issued by the configured operator, makes sure the invitee exists in the
pool and is in the group. Nothing else invites into the group: people the operator's guests
invite do NOT get provisioned.

Safety rails (the log is written by an internet-facing container, so treat it as input):
  * only invites whose inviter is the operator user id, not closed, not expired
  * strict email syntax before any value reaches the AWS CLI; arguments are a list, never a shell
  * at most MAX_PER_DAY new provisions per rolling 24 h (bounds a forged-log or email-spam abuse)
  * a host-side ledger (outside every container mount) makes it idempotent and is the audit trail
  * failures are retried with a delay and a cap, never in a tight loop

Environment: HERDR_COLLAB_OPERATOR_USER (required), HERDR_COLLAB_DATA_DIR, HERDR_COLLAB_POOL,
HERDR_COLLAB_GROUP, HERDR_COLLAB_LEDGER_DIR, HERDR_COLLAB_AUTO_CREATE (default 1: create the
Cognito user and let Cognito email them), HERDR_COLLAB_AUTO_DOMAINS (optional comma list).
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time

MAX_PER_DAY = 5            # override: HERDR_COLLAB_MAX_PER_DAY
MAX_ATTEMPTS_PER_RUN = 5   # aws calls are slow; a forged log must not stretch one run
MAX_LOG_BYTES = 64 * 1024 * 1024
RETRY_AFTER = 600.0
MAX_ATTEMPTS = 5
_EMAIL = re.compile(r"^[a-z0-9][a-z0-9._%+\-]{0,63}@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,24}$")


class AwsCognito:
    """The few Cognito calls we need, through the aws CLI (credentials come from the host)."""

    def __init__(self, pool: str, group: str):
        self.pool, self.group, self.region = pool, group, pool.split("_", 1)[0]

    def _aws(self, *args: str) -> dict:
        out = subprocess.run(["aws", "cognito-idp", *args, "--region", self.region, "--output", "json"],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        return json.loads(out) if out.strip() else {}

    def find_user(self, email: str) -> str | None:
        """The pool has open self-signup, so an account that merely CLAIMS this email proves
        nothing. Trust only one whose email Cognito verified; refuse an ambiguous match."""
        users = self._aws("list-users", f"--user-pool-id={self.pool}", f'--filter=email = "{email}"',
                          "--limit=2").get("Users", [])
        if not users:
            return None
        if len(users) > 1:
            raise RuntimeError("more than one account has this email; resolve it by hand")
        attrs = {a["Name"]: a.get("Value") for a in users[0].get("Attributes", [])}
        if attrs.get("email_verified") != "true":
            raise RuntimeError("an account with this email exists but its email is not verified")
        return users[0]["Username"]

    def create_user(self, email: str) -> str:
        user = self._aws("admin-create-user", f"--user-pool-id={self.pool}", f"--username={email}",
                         "--user-attributes", f"Name=email,Value={email}", "Name=email_verified,Value=true",
                         "--desired-delivery-mediums", "EMAIL")["User"]
        return user["Username"]

    def add_to_group(self, username: str) -> None:
        self._aws("admin-add-user-to-group", f"--user-pool-id={self.pool}", f"--username={username}",
                  f"--group-name={self.group}")


def open_invites(log_path: str, operator: str, now: float) -> list[dict]:
    created: dict[str, dict] = {}
    closed: set[str] = set()
    try:
        # The container can write this file: never follow a symlink, never block on a FIFO,
        # never read an absurd size.
        fd = os.open(log_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_LOG_BYTES:
            os.close(fd)
            return []
        fh = os.fdopen(fd, "rb")
    except OSError:
        return []
    with fh:
        for raw in fh.read(MAX_LOG_BYTES).split(b"\n"):
            try:
                ev = json.loads(raw)
                p = ev["payload"]
            except (ValueError, KeyError, TypeError):
                continue
            if ev.get("event_type") == "invite_created" and isinstance(p, dict):
                created[str(p.get("invite_id"))] = p
            elif ev.get("event_type") == "invite_closed" and isinstance(p, dict):
                closed.add(str(p.get("invite_id")))
    out = []
    for iid, p in created.items():
        exp = p.get("expires_at_epoch")
        if (iid in closed or p.get("actor_user_id") != operator or not isinstance(p.get("email"), str)
                or not isinstance(exp, (int, float)) or exp <= now):
            continue
        out.append({"invite_id": iid, "email": p["email"].strip().lower()})
    return out


def load_ledger(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return rows


def run(*, log_path: str, ledger_path: str, operator: str, cognito, now: float | None = None,
        auto_create: bool = True, domains: tuple[str, ...] = (), dry_run: bool = False,
        max_per_day: int = MAX_PER_DAY, say=print) -> list[dict]:
    """Returns the ledger rows written this run."""
    now = time.time() if now is None else now
    ledger = load_ledger(ledger_path)
    done_invites = {r["invite_id"] for r in ledger if r.get("result") == "added"}
    done_emails = {r["email"] for r in ledger if r.get("result") == "added"}
    attempts: dict[str, list[dict]] = {}
    for r in ledger:
        if r.get("result") == "failed":
            attempts.setdefault(r["invite_id"], []).append(r)
    budget = max_per_day - sum(1 for r in ledger if r.get("result") == "added" and now - r.get("at", 0) < 86400)
    written, tried = [], 0
    for inv in sorted(open_invites(log_path, operator, now), key=lambda i: i["invite_id"]):
        iid, email = inv["invite_id"], inv["email"]
        if iid in done_invites or email in done_emails:
            continue
        if not _EMAIL.fullmatch(email) or (domains and email.rsplit("@", 1)[1] not in domains):
            say(f"skip {iid}: email not eligible")
            continue
        tries = attempts.get(iid, [])
        if len(tries) >= MAX_ATTEMPTS or (tries and now - tries[-1]["at"] < RETRY_AFTER):
            continue
        if budget <= 0:
            say("daily provisioning cap reached; remaining invitations wait")
            break
        if tried >= MAX_ATTEMPTS_PER_RUN:
            break
        if dry_run:
            say(f"would add {email} (invite {iid})")
            continue
        tried += 1
        try:
            username = cognito.find_user(email)
            created = False
            if username is None:
                if not auto_create:
                    say(f"{email} has no account yet and auto-create is off; waiting")
                    continue
                username, created = cognito.create_user(email), True
            cognito.add_to_group(username)
            row = {"at": now, "invite_id": iid, "email": email, "result": "added", "created": created}
            budget -= 1
        except Exception as exc:                     # noqa: BLE001  (aws CLI / network: retry later)
            detail = getattr(exc, "stderr", None) or str(exc)
            row = {"at": now, "invite_id": iid, "email": email, "result": "failed", "error": str(detail)[:200]}
            say(f"provisioning {email} failed: {row['error']}")
        os.makedirs(os.path.dirname(ledger_path), mode=0o700, exist_ok=True)
        fd = os.open(ledger_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        written.append(row)
        if row["result"] == "added":
            say(f"added {email} to the group ({'created account' if row['created'] else 'existing account'})")
    return written


def main(argv: list[str]) -> int:
    operator = os.environ.get("HERDR_COLLAB_OPERATOR_USER", "")
    if not operator:
        return 0                                     # no operator configured: nothing is automatic
    home = os.path.expanduser("~")
    data = os.environ.get("HERDR_COLLAB_DATA_DIR", f"{home}/.local/share/herdr-engineering/collab")
    ledger_dir = os.environ.get("HERDR_COLLAB_LEDGER_DIR", f"{home}/.local/share/herdr-engineering/collab-provision")
    pool = os.environ.get("HERDR_COLLAB_POOL", "us-west-2_GjtcM0PCp")
    group = os.environ.get("HERDR_COLLAB_GROUP", "local.dev")
    domains = tuple(d.strip().lower() for d in os.environ.get("HERDR_COLLAB_AUTO_DOMAINS", "").split(",") if d.strip())
    run(log_path=f"{data}/collab.jsonl", ledger_path=f"{ledger_dir}/ledger.jsonl", operator=operator,
        cognito=AwsCognito(pool, group), auto_create=os.environ.get("HERDR_COLLAB_AUTO_CREATE", "1") != "0",
        domains=domains, dry_run="--dry-run" in argv,
        max_per_day=int(os.environ.get("HERDR_COLLAB_MAX_PER_DAY", MAX_PER_DAY)),
        say=lambda m: print(f"collab-provision: {m}", file=sys.stderr))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
