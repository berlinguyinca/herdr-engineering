"""A small ticket board per Rakazo space: boards, tickets and comments, kept in prefs.

Each space has one board (created on first use) with a ticket prefix and a counter. A ticket has a status (todo, doing, review,
blocked, done, closed), a priority and an optional assignee bot of this space. Comments are append-only. Nothing here is deleted:
the board has no remove, and tickets move to ``closed``. Onboarding calls are acknowledged and the signup survey is kept.
"""
from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from typing import Any

BOARD_KIND, TICKET_KIND, COMMENT_KIND, SURVEY_KIND = "board", "ticket", "ticket_comment", "signup_survey"
STATUSES = ("todo", "doing", "review", "blocked", "done", "closed")
PRIORITIES = ("low", "normal", "high", "urgent")
MAX_TICKETS = 2000
MAX_COMMENTS = 200


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Tickets:
    def __init__(self, bridge, error_type):
        self.b = bridge
        self.Error = error_type
        self.prefs = bridge.prefs

    def procs(self) -> dict[str, Any]:
        p = {"boards/list": self.boards_list, "boards/get": self.board_get, "boards/rename": self.board_rename,
             "tickets/list": self.list, "tickets/get": self.get, "tickets/create": self.create,
             "tickets/update": self.update, "tickets/move": self.move, "tickets/comment": self.comment,
             "tickets/comments": self.comments, "onboarding/signupSurvey": self.survey}
        p.update({f"onboarding/{n}": self.ack for n in ("start", "promptFocus", "choose", "dismissFocus", "appConnected")})
        return p

    def ack(self, ctx, inp):
        return {"ok": True}        # the guided tour is the app's; this server keeps no tour state

    # board --------------------------------------------------------------------------------------------------
    def _board(self, ctx) -> tuple[str, dict[str, Any]]:
        boards = self.prefs.items(ctx.account.account_id, ctx.space, BOARD_KIND)
        if not boards:
            stamp = _now()
            row = {"name": "Tickets", "ticketPrefix": "TKT", "next": 1, "createdAt": stamp, "updatedAt": stamp}
            self.b.prefs_put(ctx.account.account_id, ctx.space, BOARD_KIND, "main", row)
            boards = {"main": row}
        key = next(iter(boards))
        return "brd_" + ctx.space + "_" + key, boards[key]

    def _board_view(self, ctx, bid, row):
        return {"id": bid, "spaceId": ctx.space, "name": row["name"], "ticketPrefix": row["ticketPrefix"],
                "createdAt": row["createdAt"], "updatedAt": row["updatedAt"]}

    def boards_list(self, ctx, inp):
        return [self._board_view(ctx, *self._board(ctx))]

    def board_get(self, ctx, inp):
        return self._board_view(ctx, *self._board(ctx))

    def board_rename(self, ctx, inp):
        bid, row = self._board(ctx)
        row = dict(row)
        if "name" in inp:
            if not isinstance(inp["name"], str) or not inp["name"].strip() or len(inp["name"]) > 80:
                raise self.Error(400, "BAD_REQUEST", "A board needs a name of up to 80 characters.")
            row["name"] = " ".join(inp["name"].split())
        if "ticketPrefix" in inp:
            if not isinstance(inp["ticketPrefix"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,11}", inp["ticketPrefix"]):
                raise self.Error(400, "BAD_REQUEST", "The prefix must be 1 to 12 letters or digits, starting with a letter.")
            row["ticketPrefix"] = inp["ticketPrefix"].upper()
        row["updatedAt"] = _now()
        self.b.prefs_put(ctx.account.account_id, ctx.space, BOARD_KIND, "main", row)
        return self._board_view(ctx, bid, row)

    # tickets ------------------------------------------------------------------------------------------------
    def _view(self, ctx, key, row) -> dict[str, Any]:
        bid, board = self._board(ctx)
        return {"id": key, "boardId": bid, "spaceId": ctx.space, "number": row["number"],
                "ref": f"{row['prefix']}-{row['number']}", "title": row["title"], "description": row["description"],
                "status": row["status"], "priority": row["priority"], "assigneeBotId": row["assigneeBotId"],
                "createdByBotId": None, "createdByUserId": ctx.user, "createdAt": row["createdAt"],
                "updatedAt": row["updatedAt"], "completedAt": row["completedAt"]}

    def _all(self, ctx):
        return self.prefs.items(ctx.account.account_id, ctx.space, TICKET_KIND)

    def _find(self, ctx, inp):
        rows = self._all(ctx)
        key = inp.get("id") or inp.get("ticketId")
        if key in rows:
            return key, rows[key]
        ref = (inp.get("ref") or "").upper()
        for k, r in rows.items():
            if ref and f"{r['prefix']}-{r['number']}".upper() == ref:
                return k, r
        raise self.Error(404, "NOT_FOUND", "Not found.")

    def _assignee(self, ctx, value):
        if value is None:
            return None
        return self.b._assistant(ctx, value).assistant_id

    def list(self, ctx, inp):
        status, bot, q = inp.get("status"), inp.get("assigneeBotId"), (inp.get("q") or "").casefold()
        if status is not None and status not in STATUSES:
            raise self.Error(400, "BAD_REQUEST", "Unknown status.")
        if not isinstance(inp.get("q", ""), str) or len(inp.get("q", "")) > 200:
            raise self.Error(400, "BAD_REQUEST", "q is too long.")
        out = [self._view(ctx, k, r) for k, r in self._all(ctx).items()
               if (status is None or r["status"] == status) and (bot is None or r["assigneeBotId"] == bot)
               and (not q or q in (r["title"] + " " + (r["description"] or "")).casefold())]
        return {"tickets": sorted(out, key=lambda t: -t["number"])}

    def get(self, ctx, inp):
        return self._view(ctx, *self._find(ctx, inp))

    def _text(self, inp, key, limit, *, required=False):
        v = inp.get(key)
        if v is None and not required:
            return None
        if not isinstance(v, str) or (required and not v.strip()) or len(v) > limit:
            raise self.Error(400, "BAD_REQUEST", f"{key} must be text of up to {limit} characters.")
        return v

    def create(self, ctx, inp):
        title = self._text(inp, "title", 200, required=True).strip()
        description = self._text(inp, "description", 20000)
        priority = inp.get("priority", "normal")
        if priority not in PRIORITIES:
            raise self.Error(400, "BAD_REQUEST", "Unknown priority.")
        assignee = self._assignee(ctx, inp.get("assigneeBotId"))
        if len(self._all(ctx)) >= MAX_TICKETS:
            raise self.Error(400, "BAD_REQUEST", "This board is full.")
        _, board = self._board(ctx)
        stamp, key = _now(), "tkt" + secrets.token_hex(8)
        row = {"number": board["next"], "prefix": board["ticketPrefix"], "title": title, "description": description,
               "status": "todo", "priority": priority, "assigneeBotId": assignee, "createdAt": stamp, "updatedAt": stamp,
               "completedAt": None}
        self.b.prefs_put(ctx.account.account_id, ctx.space, TICKET_KIND, key, row)
        self.b.prefs_put(ctx.account.account_id, ctx.space, BOARD_KIND, "main", {**board, "next": board["next"] + 1})
        return self._view(ctx, key, row)

    def _save(self, ctx, key, row):
        row["updatedAt"] = _now()
        row["completedAt"] = row["updatedAt"] if row["status"] in ("done", "closed") else None
        self.b.prefs_put(ctx.account.account_id, ctx.space, TICKET_KIND, key, row)
        return self._view(ctx, key, row)

    def update(self, ctx, inp):
        key, row = self._find(ctx, inp)
        row = dict(row)
        if "title" in inp:
            row["title"] = self._text(inp, "title", 200, required=True).strip()
        if "description" in inp:
            row["description"] = self._text(inp, "description", 20000)
        if "priority" in inp:
            if inp["priority"] not in (*PRIORITIES, None):
                raise self.Error(400, "BAD_REQUEST", "Unknown priority.")
            row["priority"] = inp["priority"] or "normal"
        if "status" in inp:
            if inp["status"] not in STATUSES:
                raise self.Error(400, "BAD_REQUEST", "Unknown status.")
            row["status"] = inp["status"]
        if "assigneeBotId" in inp:
            row["assigneeBotId"] = self._assignee(ctx, inp["assigneeBotId"])
        return self._save(ctx, key, row)

    def move(self, ctx, inp):
        if inp.get("status") not in STATUSES:
            raise self.Error(400, "BAD_REQUEST", "Unknown status.")
        key, row = self._find(ctx, inp)
        return self._save(ctx, key, {**row, "status": inp["status"]})

    # comments -----------------------------------------------------------------------------------------------
    def _comment_view(self, key, row):
        return {"id": key, "ticketId": row["ticketId"], "body": row["body"], "authorBotId": None,
                "authorUserId": row["author"], "authorName": row["authorName"], "createdAt": row["createdAt"],
                "updatedAt": row["createdAt"]}

    def comment(self, ctx, inp):
        tkey, _ = self._find(ctx, {"id": inp.get("ticketId")})
        body = self._text(inp, "body", 10000, required=True)
        mine = [k for k, r in self.prefs.items(ctx.account.account_id, ctx.space, COMMENT_KIND).items()
                if r["ticketId"] == tkey]
        if len(mine) >= MAX_COMMENTS:
            raise self.Error(400, "BAD_REQUEST", "This ticket has too many comments.")
        key = "cmt" + secrets.token_hex(8)
        row = {"ticketId": tkey, "body": body, "author": ctx.user, "authorName": ctx.account.email.split("@", 1)[0],
               "createdAt": _now()}
        self.b.prefs_put(ctx.account.account_id, ctx.space, COMMENT_KIND, key, row)
        return self._comment_view(key, row)

    def comments(self, ctx, inp):
        tkey, _ = self._find(ctx, {"id": inp.get("ticketId")})
        rows = [(k, r) for k, r in self.prefs.items(ctx.account.account_id, ctx.space, COMMENT_KIND).items()
                if r["ticketId"] == tkey]
        return [self._comment_view(k, r) for k, r in sorted(rows, key=lambda kr: (kr[1]["createdAt"], kr[0]))]

    # onboarding ---------------------------------------------------------------------------------------------
    def survey(self, ctx, inp):
        answers = {k: inp[k] for k in ("reason", "use", "teamSize", "source") if k in inp}
        if (answers.get("use") not in (None, "personal", "work", "both")
                or answers.get("teamSize") not in (None, "1", "2-10", "11-50", "51-200", "201+")
                or any(not isinstance(answers.get(k, ""), str) or len(answers.get(k, "")) > lim
                       for k, lim in (("reason", 2000), ("source", 500)))):
            raise self.Error(400, "BAD_REQUEST", "The survey answers are not valid.")
        self.b.prefs_put(ctx.account.account_id, "-", SURVEY_KIND, "answers", {**answers, "at": _now()})
        return {"ok": True}
