"""Synchronous facade over the async Work Graph store, plus the HTTP-neutral request handler (unified workspace P02).

``WorkGraphService`` runs the asyncpg pool, the projection worker and every query on one dedicated event loop (the
``bridge._LoopRunner`` pattern) so the threaded HTTP server can call it.  ``handle`` maps one request (already
authenticated, with the graph principal) to ``(status, body, headers)``; both front doors (keyed ``/api/v1/graph/work/*``
and the HCN ``graph/work/*``) call it, so they cannot drift.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from . import workspace_contracts as wc
from .work_graph_store import SnapshotRequired, WorkGraphStore, init_connection
from .work_graph_sync import PgSources, WorkGraphReconciler, WorkGraphWorker

log = logging.getLogger(__name__)


class Unavailable(RuntimeError):
    """No usable database (collab-only mode, migration not applied)."""


class WorkGraphService:
    def __init__(self, dsn: str | None, *, artifact_ok: Callable[[Mapping[str, Any], str], bool] | None = None,
                 interval: float = 5.0, autostart: bool = True) -> None:
        self.dsn = dsn
        self.artifact_ok = artifact_ok
        self.interval = interval
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pool = None
        self.store: WorkGraphStore | None = None
        self.worker: WorkGraphWorker | None = None
        self._task = None
        self._error: str | None = None
        if autostart and dsn:
            threading.Thread(target=self._boot, name="herdr-work-graph-boot", daemon=True).start()

    # ---- lifecycle ---------------------------------------------------------------------------------------------
    def _start_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="herdr-work-graph-loop", daemon=True).start()
                self._loop = loop
            return self._loop

    def _run(self, coro, timeout: float = 60.0):
        return asyncio.run_coroutine_threadsafe(coro, self._start_loop()).result(timeout)

    async def _connect(self) -> None:
        import asyncpg
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=6, init=init_connection, command_timeout=30)
        have = await self._pool.fetchval("SELECT to_regclass('wg_entity') IS NOT NULL")
        if not have:
            await self._pool.close()
            self._pool = None
            raise Unavailable("work graph tables are not migrated (apply migration 0032)")
        self.store = WorkGraphStore(self._pool, artifact_ok=self.artifact_ok)

    def _boot(self) -> None:
        try:
            self._run(self._connect())
            self.worker = WorkGraphWorker(self.store, PgSources(self._pool), interval=self.interval)
            self._task = asyncio.run_coroutine_threadsafe(self.worker.run(), self._start_loop())
        except Exception as exc:                                      # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("work graph unavailable: %s", self._error)

    def available(self) -> bool:
        return self.store is not None

    def close(self) -> None:
        if self.worker:
            self.worker.stop()
        if self._pool is not None and self._loop is not None:
            try:
                self._run(self._pool.close(), 10)
            except Exception:                                         # noqa: BLE001
                pass

    def _store(self) -> WorkGraphStore:
        if self.store is None:
            if self.dsn and self._pool is None and self._error is None:
                try:
                    self._run(self._connect())
                except Exception as exc:                              # noqa: BLE001
                    self._error = f"{type(exc).__name__}: {exc}"[:200]
            if self.store is None:
                raise Unavailable(self._error or "no database configured (control_plane.pg_dsn)")
        return self.store

    # ---- operations (sync) ---------------------------------------------------------------------------------------
    def call(self, name: str, *args, **kwargs):
        return self._run(getattr(self._store(), name)(*args, **kwargs))

    def reconcile(self) -> dict:
        s = self._store()
        return self._run(WorkGraphReconciler(s, PgSources(self._pool)).reconcile(), 600)

    def rebuild(self, *, force: bool = False) -> dict:
        s = self._store()
        return self._run(WorkGraphReconciler(s, PgSources(self._pool)).rebuild(force=force), 1800)

    def status(self) -> dict:
        out = self._run(self._store().status())
        out["worker"] = {"steps": self.worker.steps if self.worker else 0, "last_error": self.worker.last_error if self.worker else None}
        return out


# --------------------------------------------------------------------------- request handling
def _csv(value: str | None) -> list[str]:
    return [x for x in (value or "").split(",") if x]


def handle(service: WorkGraphService | None, principal: Mapping[str, Any] | None, method: str, parts: list[str],
           query: Mapping[str, str], body: Mapping[str, Any] | None = None, if_match: str | None = None
           ) -> tuple[int, dict, dict]:
    """``parts`` is the path after ``graph/work``.  Every miss, hidden or not, is the same 404."""
    if service is None:
        return 404, {"error": "not found"}, {}
    q = query.get
    try:
        s = service
        if method == "GET":
            if parts == ["entities"]:
                return 200, s.call("list_entities", principal, kinds=_csv(q("kind")), project_id=q("project_id") or None,
                                   status=q("status") or None, include_tombstoned=q("include_tombstoned") == "true",
                                   as_of=q("as_of") or None, limit=q("limit"), cursor=q("cursor")), {}
            if parts == ["entity"]:
                doc = s.call("get_entity", principal, q("ref") or "", as_of=q("as_of") or None)
                return 200, doc, {"ETag": doc["etag"]}
            if parts == ["search"]:
                return 200, s.call("search", principal, q("q") or "", kinds=_csv(q("kind")), project_id=q("project_id") or None,
                                   limit=q("limit"), cursor=q("cursor")), {}
            if parts == ["snapshot"]:
                return 200, s.call("snapshot", principal, q("root") or "", depth=q("depth") or 2, max_nodes=q("max_nodes") or 200,
                                   edge_types=_csv(q("edge_types")) or None, as_of=q("as_of") or None), {}
            if len(parts) == 2 and parts[0] in ("projects", "issues", "tasks"):
                kind = {"projects": "project", "issues": "issue", "tasks": "task"}[parts[0]]
                return 200, s.call("centric", principal, f"{kind}:{parts[1]}", depth=q("depth") or 2,
                                   max_nodes=q("max_nodes") or 400, as_of=q("as_of") or None), {}
            if parts == ["centric"]:
                return 200, s.call("centric", principal, q("ref") or "", depth=q("depth") or 2, max_nodes=q("max_nodes") or 400,
                                   as_of=q("as_of") or None), {}
            if parts == ["facts"]:
                return 200, s.call("facts", principal, q("ref") or "", fact_kind=q("kind") or None, as_of=q("as_of") or None,
                                   limit=q("limit")), {}
            if parts == ["layout"]:
                doc = s.call("get_layout", principal, q("ref") or "", which=q("which") or "effective")
                return 200, doc, {"ETag": doc["etag"]}
            if parts == ["status"]:
                if (principal or {}).get("kind") != "operator":
                    return 404, {"error": "not found"}, {}
                return 200, s.status(), {}
        elif method in ("PUT", "POST"):
            body = body or {}
            if parts == ["layout"]:
                doc = s.call("put_layout", principal, str(body.get("ref") or q("ref") or ""), body.get("document"),
                             shared=body.get("shared") is True, if_match=if_match or body.get("if_match"))
                return 200, doc, {"ETag": doc["etag"]}
            if parts == ["reconcile"] and method == "POST":
                if (principal or {}).get("kind") != "operator":
                    return 404, {"error": "not found"}, {}
                return 200, s.reconcile(), {}
    except wc.NotFoundOrForbidden:
        return 404, {"error": "not found"}, {}
    except wc.PreconditionRequired:
        return 428, {"error": "If-Match required"}, {}
    except wc.PreconditionFailed:
        return 412, {"error": "stale etag; re-read the document"}, {}
    except SnapshotRequired as exc:
        return 409, {"error": str(exc), "snapshot_required": True}, {}
    except Unavailable as exc:
        return 503, {"error": f"work graph unavailable: {exc}"}, {}
    except (ValueError, KeyError, TypeError) as exc:                  # includes ContractError
        return 400, {"error": str(exc) if isinstance(exc, ValueError) else "bad request"}, {}
    except Exception:                                                 # noqa: BLE001 - never echo internals
        log.exception("work graph request failed")
        return 500, {"error": "internal error"}, {}
    return 404, {"error": "not found"}, {}


def encode(body: Mapping[str, Any]) -> bytes:
    return json.dumps(body).encode()
