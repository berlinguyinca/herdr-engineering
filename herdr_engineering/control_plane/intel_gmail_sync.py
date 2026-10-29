"""Gmail sync engine over an injected client (cursor, backfill, deletion, revoke).

The engine never holds credentials and never calls Google itself: a ``client`` object does (blocker B1 supplies
the OAuth client and consent). With a fake client the whole lifecycle is testable offline. Read-only by design:
the client protocol has no send, label, delete or modify call, so this module cannot change a mailbox.

client protocol::
    profile() -> {"history_id": str}
    list_recent(days: int, limit: int) -> list[str]          # message ids, newest first
    history(since: str) -> {"history_id": str, "added": [id], "deleted": [id]}   # raises HistoryExpired
    get_message(id: str) -> dict | None                      # None when it no longer exists
Raising ``AuthRevoked`` from any call means the user withdrew consent.
"""
from __future__ import annotations

import logging

from . import intel_gmail
from . import project_intelligence as pi

log = logging.getLogger(__name__)


class HistoryExpired(Exception):
    """The stored cursor is too old for Gmail to replay; re-anchor with a bounded backfill."""


class AuthRevoked(Exception):
    """Consent was withdrawn or the token was revoked."""


class GmailSync:
    def __init__(self, client, store: pi.IntelligenceStore, cfg_fn, *, org_id: str, account: str,
                 project_ids: tuple[str, ...], cursor_get=lambda: None, cursor_set=lambda v: None,
                 security_label: str = "confidential", backfill_days: int = 30, max_per_run: int = 200):
        self._c, self._store, self._cfg_fn = client, store, cfg_fn
        self._org, self._account, self._projects, self._label = org_id, account, tuple(project_ids), security_label
        self._cursor_get, self._cursor_set = cursor_get, cursor_set
        self._days, self._max = backfill_days, max_per_run
        self.stats = {"messages": 0, "events": 0, "links": 0, "deleted": 0, "skipped": 0}

    def _enabled(self) -> dict | None:
        cfg = pi.config(self._cfg_fn())
        return cfg if cfg["enabled"] and cfg["ingest"] and not cfg["kill_switch"] else None

    def _store_message(self, msg: dict, cfg: dict) -> None:
        for raw in intel_gmail.normalise_message(msg, org_id=self._org, account=self._account,
                                                  project_ids=self._projects, security_label=self._label):
            _, created = self._store.append_event(raw, cfg=cfg)
            self.stats["events"] += int(created)
        if cfg["derive"]:
            for link in intel_gmail.explicit_links(msg, org_id=self._org, project_ids=self._projects, account=self._account):
                try:
                    self._store.add_relationship(link, cfg=cfg, actor="gmail-sync")
                    self.stats["links"] += 1
                except pi.IntelligenceError:
                    self.stats["skipped"] += 1
        self.stats["messages"] += 1

    def backfill(self, *, dry_run: bool = True) -> dict:
        """Bounded, repeatable, replay-safe. ``dry_run`` (default) only counts what a run would fetch."""
        cfg = self._enabled()
        if cfg is None:
            return {"status": "off"}
        try:
            anchor = self._c.profile()["history_id"]
            ids = list(self._c.list_recent(self._days, self._max))[: self._max]
            if dry_run:
                return {"status": "dry_run", "would_fetch": len(ids)}
            for mid in ids:
                msg = self._c.get_message(mid)
                if msg is not None:
                    self._store_message(msg, cfg)
        except AuthRevoked:
            return self._revoked()
        except (pi.IngestionPaused, pi.Conflict):
            return {"status": "paused"}       # cursor untouched: the next run repeats safely
        self._cursor_set(anchor)               # only after every fetched message is stored
        return {"status": "ok", "fetched": len(ids)}

    def poll(self) -> dict:
        """Incremental sync from the stored cursor. An expired cursor re-anchors with a bounded backfill."""
        cfg = self._enabled()
        if cfg is None:
            return {"status": "off"}
        cursor = self._cursor_get()
        if cursor is None:
            return self.backfill(dry_run=False)
        try:
            page = self._c.history(cursor)
            for mid in list(page.get("added") or [])[: self._max]:
                msg = self._c.get_message(mid)
                if msg is not None:
                    self._store_message(msg, cfg)
            for mid in page.get("deleted") or []:
                self.stats["deleted"] += self._store.tombstone_source(self._org, f"gmail:{self._account}:{mid}")
        except HistoryExpired:
            return self.backfill(dry_run=False)
        except AuthRevoked:
            return self._revoked()
        except (pi.IngestionPaused, pi.Conflict):
            return {"status": "paused"}
        self._cursor_set(page["history_id"])
        return {"status": "ok", "added": len(page.get("added") or []), "deleted": len(page.get("deleted") or [])}

    def _revoked(self) -> dict:
        """Consent withdrawn: stop the source, forget the cursor and tombstone what was ingested for this account."""
        self._store.pause_source(self._org, "gmail", True)
        self._cursor_set(None)
        n = 0
        for rec in self._store.account_message_refs(self._org, self._account):
            n += self._store.tombstone_source(self._org, rec)
        log.warning("gmail consent revoked; source paused and %d event(s) tombstoned", n)
        return {"status": "revoked", "tombstoned": n}
