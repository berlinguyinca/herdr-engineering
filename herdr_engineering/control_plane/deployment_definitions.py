"""Pipeline definitions and release targets for the deployment coordinator (deploy-4 Task 2).

Definitions are owned by ``InferWeave/inferweave`` (``deploy/pipelines/<component>.yaml``,
plan 1). Herdr reads a checkout of that directory, named by the fleet config key
``control_plane.deployment_definitions`` (an absolute path; absent = no definitions).

* The validator is a port of ``deploy/pipelines/validate.py`` with the same rules and
  messages; keep the two in sync. A set loads all-or-nothing: any error and the
  previous good set stays in use (or, if there never was one, none is used).
* Parsing is a ``yaml.SafeLoader`` subclass that also refuses anchors and aliases
  (alias bombs); python object tags are refused, never built. Deep nesting, odd files
  (symlinks, FIFOs, devices: lstat + O_NOFOLLOW|O_NONBLOCK) and a directory or file
  writable by group/other (definitions are an authorization input) are load errors.
  Messages never echo file content, interpolated values are capped, and the stored
  error list is bounded. Herdr-only additions to validate.py: a .yaml and .yml with the
  same stem are a duplicate; anchors/aliases are refused; the optional ``requires: [{repo,
  ci_context, stage}]`` (stages of OTHER follows tracks the coordinator waits for) is
  validated here, including the cross-file checks (a followed track, an existing stage, not
  the own track, no cycle). validate.py must learn the key before a definition uses it.
* ``target_for(component)`` is the newest green ``main`` push of the definition's
  ``follows.repo``, read from the CI monitor's last snapshot (no provider calls here).
  A pipeline-level ``success`` means every workflow passed, so it is stricter than the
  ``follows.ci_context`` status alone (whose per-workflow state the list rows lack).
  Caveat: "a rerun never moves the target back" ranks a commit by the earliest run
  herdr has SEEN for it. A server that starts after an old commit's original run has
  left the CI window sees only the rerun and takes it as newest. The coordinator
  (Task 3) must guard on CI freshness / ancestry before acting (a ``target_off_main``
  style check is not built here).
* Each ``follows`` track has a plan: a new green commit bumps ``generation`` and marks
  the plan restarted from the newest commit. A rerun of an older commit, a CI outage or
  an empty snapshot never moves the target or bumps the generation.
* Ingest authorization: with definitions loaded, a host may report only components
  whose ``host`` is its fleet id. Without any configured, every host may report every
  component (the Task 1 behaviour) and ``status()['warning']`` says so. Configured but
  never valid fails closed.

Snapshots are last-known observations, never execution authority.
"""
from __future__ import annotations

import copy
import os
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

import yaml

# ------------------------------------------------- validator (port of validate.py)
RESTARTS = {
    "container-recreate",
    "process-restart-in-allocation",
    "gateway-cycle",
    "systemd-restart",
}
GPU_RELEASING = {"job-cancel-resubmit", "scancel-resubmit"}
PROBES = {"served-request"}
STRATEGIES = {"previous-version", "none"}
TOP = {"component", "stage", "order", "host", "updater", "follows", "restart",
       "gpu", "health", "rollback"}
#: Herdr-side optional keys (not in validate.py yet): absent = the Task 2 behaviour.
OPTIONAL = {"requires"}
REQUIRES = {"repo", "ci_context", "stage"}
MAX_REQUIRES = 8
FOLLOWS = {"repo", "tag", "asset", "branch", "ci_context"}
HEALTH = {"probe", "timeout_s", "hold_s"}
ROLLBACK = {"strategy", "reason"}

#: Herdr-side bounds (not in validate.py): a definition is a few hundred bytes.
MAX_FILE_BYTES = 64 * 1024
MAX_FILES = 256
#: Commits remembered per track to rank reruns (bounded, oldest dropped).
RANK_MEMORY = 2048
#: Message bounds: one interpolated value, one message, the stored error list.
MAX_VALUE_REPR = 64
MAX_MESSAGE = 240
MAX_ERRORS = 100
WRITABLE_BY_OTHERS = "must not be writable by group or other (chmod go-w)"
CONFIG_KEY = "deployment_definitions"
UNRESTRICTED_WARNING = "no deployment definitions loaded: any fleet host may report any component"
INVALID_WARNING = "deployment definitions are configured but invalid: deployment events are refused"
STALE_WARNING = "deployment definitions have errors: the last good set is still in use"


def _r(v):
    """repr(v), capped: a message never carries more than MAX_VALUE_REPR characters of input."""
    text = repr(v)
    return text if len(text) <= MAX_VALUE_REPR else text[:MAX_VALUE_REPR - 3] + "..."


def _k(k):
    text = str(k)
    return text if len(text) <= MAX_VALUE_REPR else text[:MAX_VALUE_REPR - 3] + "..."


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _str(v):
    return isinstance(v, str) and v != ""


def _section(doc, key, allowed, required, errs):
    if key not in doc:
        return None
    sec = doc[key]
    if not isinstance(sec, dict):
        errs.append(f"{key} must be a mapping")
        return None
    for k in sorted(sec, key=str):
        if k not in allowed:
            errs.append(f"unknown key {key}.{_k(k)}")
    for k in sorted(required - set(sec)):
        errs.append(f"missing key {key}.{k}")
    return sec


def validate_definition(doc, name):
    """Errors for one definition (``name`` is the file stem); [] when valid."""
    errs = []
    if not isinstance(doc, dict):
        return ["definition must be a mapping"]
    for k in sorted(doc, key=str):
        if k not in TOP and k not in OPTIONAL:
            errs.append(f"unknown key {_k(k)}")
    for k in sorted(TOP - set(doc)):
        errs.append(f"missing key {k}")

    if "component" in doc and doc["component"] != name:
        errs.append(f"component {_r(doc['component'])} must match file name {_r(name)}")
    if "stage" in doc and (not _int(doc["stage"]) or doc["stage"] not in (1, 2)):
        errs.append("stage must be 1 or 2")
    if "order" in doc and (not _int(doc["order"]) or doc["order"] < 0):
        errs.append("order must be an integer >= 0")
    for k in ("host", "updater"):
        if k in doc and not _str(doc[k]):
            errs.append(f"{k} must be a non-empty string")
    if "gpu" in doc and not isinstance(doc["gpu"], bool):
        errs.append("gpu must be a boolean")

    f = _section(doc, "follows", FOLLOWS, FOLLOWS, errs)
    if f is not None:
        for k in ("repo", "tag", "asset", "ci_context"):
            if k in f and not _str(f[k]):
                errs.append(f"follows.{k} must be a non-empty string")
        if "branch" in f and f["branch"] != "main":
            errs.append("follows.branch must be 'main'")

    if "restart" in doc:
        r = doc["restart"]
        if isinstance(r, str) and r in GPU_RELEASING and doc.get("gpu") is True:
            errs.append(f"restart {_r(r)} releases a GPU; not allowed when gpu is true")
        if not isinstance(r, str) or r not in RESTARTS:
            errs.append(f"restart {_r(r)} must be one of {sorted(RESTARTS)}")

    h = _section(doc, "health", HEALTH, HEALTH, errs)
    if h is not None:
        if "probe" in h and (not isinstance(h["probe"], str) or h["probe"] not in PROBES):
            errs.append("health.probe must be 'served-request'")
        for k in ("timeout_s", "hold_s"):
            if k in h and (not _int(h[k]) or h[k] < 0):
                errs.append(f"health.{k} must be an integer >= 0")

    rb = _section(doc, "rollback", ROLLBACK, {"strategy"}, errs)
    if rb is not None and "strategy" in rb:
        if not isinstance(rb["strategy"], str) or rb["strategy"] not in STRATEGIES:
            errs.append(f"rollback.strategy must be one of {sorted(STRATEGIES)}")
        elif rb["strategy"] == "none" and not _str(rb.get("reason")):
            errs.append("rollback.reason is required when strategy is 'none'")
    if "requires" in doc:
        _validate_requires(doc["requires"], errs)
    return errs


def _validate_requires(value, errs):
    """``requires: [{repo, ci_context, stage}]``: stages of OTHER follows tracks (branch is
    always main) that must be rolled out on their target before this track may start."""
    if not isinstance(value, list):
        errs.append("requires must be a list")
        return
    if len(value) > MAX_REQUIRES:
        errs.append(f"requires must have at most {MAX_REQUIRES} entries")
        return
    seen = set()
    for i, r in enumerate(value):
        where = f"requires[{i}]"
        if not isinstance(r, dict):
            errs.append(f"{where} must be a mapping")
            continue
        for k in sorted(r, key=str):
            if k not in REQUIRES:
                errs.append(f"unknown key {where}.{_k(k)}")
        for k in sorted(REQUIRES - set(r)):
            errs.append(f"missing key {where}.{k}")
        for k in ("repo", "ci_context"):
            if k in r and not _str(r[k]):
                errs.append(f"{where}.{k} must be a non-empty string")
        if "stage" in r and (not _int(r["stage"]) or r["stage"] not in (1, 2)):
            errs.append(f"{where}.stage must be 1 or 2")
        key = _requirement(r)
        if key is not None:
            if key in seen:
                errs.append(f"{where} duplicates an earlier requirement")
            seen.add(key)


def _requirement(r):
    """((repo, "main", ci_context), stage) for a well-formed entry, else None."""
    if (not isinstance(r, dict) or set(r) != REQUIRES or not _str(r["repo"])
            or not _str(r["ci_context"]) or not _int(r["stage"]) or r["stage"] not in (1, 2)):
        return None
    return ((r["repo"], "main", r["ci_context"]), r["stage"])


def _doc_track(doc):
    f = doc.get("follows") if isinstance(doc, dict) else None
    if not isinstance(f, dict) or not all(_str(f.get(k)) for k in ("repo", "ci_context")):
        return None
    return (f["repo"], "main", f["ci_context"])


def _requires_errors(docs):
    """Cross-file ``requires`` checks: a requirement names another track some definition
    follows, at a stage that track has, and the track graph has no cycle."""
    errs = []
    stages: dict[tuple, set] = {}
    for _stem, (_fn, doc) in docs.items():
        track = _doc_track(doc)
        if track is not None and _int(doc.get("stage")):
            stages.setdefault(track, set()).add(doc["stage"])
    edges: dict[tuple, set] = {}
    for _stem, (fn, doc) in sorted(docs.items()):
        reqs = doc.get("requires") if isinstance(doc, dict) else None
        track = _doc_track(doc)
        if not isinstance(reqs, list) or track is None:
            continue
        for i, r in enumerate(reqs):
            key = _requirement(r)
            if key is None:
                continue
            req_track, stage = key
            if req_track == track:
                errs.append(f"{fn}: requires[{i}] names its own track")
            elif req_track not in stages:
                errs.append(f"{fn}: requires[{i}] names a track no definition follows")
            elif stage not in stages[req_track]:
                errs.append(f"{fn}: requires[{i}] names stage {stage}, which that track does not have")
            else:
                edges.setdefault(track, set()).add(req_track)
    # cycle check over the track graph (white/grey/black DFS, iterative)
    state: dict[tuple, int] = {}
    for start in sorted(edges):
        if state.get(start):
            continue
        stack = [(start, iter(sorted(edges.get(start, ()))))]
        state[start] = 1
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                errs.append(f"requires forms a cycle between tracks {_r(node[0])} and {_r(nxt[0])}")
                return errs
            elif not state.get(nxt):
                state[nxt] = 1
                stack.append((nxt, iter(sorted(edges.get(nxt, ())))))
    return errs


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses anchors and aliases: definitions never need them, and an
    alias chain turns a few hundred bytes into megabytes (a "billion laughs" file)."""

    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise yaml.composer.ComposerError(
                None, None, "YAML anchors and aliases are not allowed", event.start_mark)
        return super().compose_node(parent, index)


def _parse_error(exc):
    """A short parse message without the file snippet (it can hold a secret)."""
    if isinstance(exc, RecursionError):
        return "nested too deeply"
    if isinstance(exc, MemoryError):
        return "too large to parse"
    if isinstance(exc, yaml.MarkedYAMLError):
        what = exc.problem or exc.context or "invalid YAML"
        mark = exc.problem_mark or exc.context_mark
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        return f"{what}{where}"
    if isinstance(exc, yaml.YAMLError):
        return "invalid YAML"
    if isinstance(exc, UnicodeDecodeError):
        return "not UTF-8"
    return str(exc)


def _read_file(path, fn):
    """(doc, error) for one entry: regular files only, never followed, never blocking."""
    full = os.path.join(path, fn)
    try:
        st = os.lstat(full)
        if not stat.S_ISREG(st.st_mode):
            return None, f"{fn}: not a regular file"
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return None, f"{fn}: {WRITABLE_BY_OTHERS}"
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:   # ELOOP: became a symlink after the lstat
        return None, f"{fn}: cannot parse: {e.strerror or 'unreadable'}"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):   # swapped after the lstat
            return None, f"{fn}: not a regular file"
        with os.fdopen(fd, "rb") as fh:
            fd = None
            raw = fh.read(MAX_FILE_BYTES + 1)
    except OSError as e:
        return None, f"{fn}: cannot parse: {e.strerror or 'unreadable'}"
    finally:
        if fd is not None:
            os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        return None, f"{fn}: cannot parse: file exceeds {MAX_FILE_BYTES} bytes"
    try:
        return yaml.load(raw.decode("utf-8"), Loader=_NoAliasLoader), None   # noqa: S506 - SafeLoader subclass
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError, MemoryError, ValueError) as e:
        return None, f"{fn}: cannot parse: {_parse_error(e)}"


def _bound(errs):
    """Cap each message and the list (the page shows them; the input is untrusted)."""
    out = [e if len(e) <= MAX_MESSAGE else e[:MAX_MESSAGE - 3] + "..." for e in errs[:MAX_ERRORS]]
    if len(errs) > MAX_ERRORS:
        out.append(f"... {len(errs) - MAX_ERRORS} more errors")
    return out


def _read_dir(path):
    """(errors, {stem: (file name, doc)}) with validate_dir's messages and order."""
    errs, docs, seen, stems = [], {}, {}, {}
    try:
        st = os.stat(path)
    except OSError:
        st = None
    if st is None or not stat.S_ISDIR(st.st_mode):
        return _bound([f"{path}: not a directory"]), docs
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return _bound([f"{path}: {WRITABLE_BY_OTHERS}"]), docs
    try:
        names = sorted(n for n in os.listdir(path) if n.endswith((".yaml", ".yml")))
    except OSError as e:
        return _bound([f"{path}: cannot list: {e.strerror or 'error'}"]), docs
    if len(names) > MAX_FILES:
        return _bound([f"{path}: more than {MAX_FILES} definition files"]), docs
    for fn in names:
        stem = os.path.splitext(fn)[0]
        if stem in stems:
            errs.append(f"{fn}: duplicate component {_r(stem)} also in {stems[stem]}")
            continue
        stems[stem] = fn
        doc, error = _read_file(path, fn)
        if error:
            errs.append(error)
            continue
        errs.extend(f"{fn}: {m}" for m in validate_definition(doc, stem))
        if isinstance(doc, dict) and _int(doc.get("stage")) and _int(doc.get("order")) \
                and isinstance(doc.get("host"), str):
            key = (doc["host"], doc["stage"], doc["order"])
            if key in seen:
                errs.append(f"{fn}: duplicate (host, stage, order) {_r(key)} also in {seen[key]}")
            else:
                seen[key] = fn
        docs[stem] = (fn, doc)
    errs.extend(_requires_errors(docs))
    return _bound(errs), docs


def validate_dir(path):
    """All errors for a definitions directory, as plan 1's ``validate_dir`` reports them."""
    return _read_dir(path)[0]


# ----------------------------------------------------------------- the model
@dataclass(frozen=True)
class Definition:
    component: str
    stage: int
    order: int
    host: str
    updater: str
    follows: dict
    restart: str
    gpu: bool
    health: dict
    rollback: dict
    #: ((repo, branch, ci_context), stage) pairs of OTHER tracks that must be rolled out first.
    requires: tuple = ()

    @property
    def hold_s(self) -> int:
        return self.health["hold_s"]

    @property
    def track(self) -> tuple:
        """What the component follows: (repo, branch, ci_context)."""
        return (self.follows["repo"], self.follows["branch"], self.follows["ci_context"])


@dataclass(frozen=True)
class Target:
    component: str
    commit: str
    run_number: int
    repository: str
    branch: str
    ci_context: str
    tag: str
    asset: str
    generation: int


@dataclass(frozen=True)
class Plan:
    """The roll toward one track's newest green commit. ``generation`` grows by one each
    time a newer green commit replaces the target; ``restarted`` is True from then on."""
    track: tuple
    target_commit: str | None
    run_number: int | None
    generation: int
    restarted: bool
    previous_commit: str | None
    groups: list = field(default_factory=list)


def load_definitions(path):
    """({component: Definition}, errors). All-or-nothing: any error gives {}."""
    errs, docs = _read_dir(path)
    if errs:
        return {}, errs
    out = {}
    for stem, (_fn, d) in docs.items():
        out[stem] = Definition(
            component=d["component"], stage=d["stage"], order=d["order"], host=d["host"],
            updater=d["updater"], follows=dict(d["follows"]), restart=d["restart"],
            gpu=d["gpu"], health=dict(d["health"]), rollback=dict(d["rollback"]),
            requires=tuple(sorted(_requirement(r) for r in d.get("requires") or ())))
    return out, []


def _sorted(defs):
    return sorted(defs, key=lambda d: (d.stage, d.order, d.component))


def _push_event(ci_context):
    """``ci/woodpecker/<event>/<workflow>`` -> the Woodpecker event (default push)."""
    parts = ci_context.split("/")
    return parts[2] if len(parts) >= 4 and parts[0] == "ci" and parts[1] == "woodpecker" else "push"


class DeploymentAuthorizationError(PermissionError):
    """The reporting host may not report this component (HTTP 403)."""


class DeploymentDefinitionsUnavailable(RuntimeError):
    """Definitions are configured but none valid was ever loaded (HTTP 503, fail closed)."""


class DeploymentDefinitions:
    """Live view of the definitions directory plus per-track release plans.

    ``path_fn`` returns the configured value (None/"" = not configured); it is called on
    every read so a fleet-config change applies without a restart. ``ci_snapshot`` returns
    a CIMonitor-shaped snapshot (``{'pipelines': {group: [row]}}``).
    """

    def __init__(self, path_fn, *, ci_snapshot=None):
        self._path_fn = path_fn
        self._ci_snapshot = ci_snapshot
        self._lock = threading.RLock()
        self._sig = None
        self._defs: dict[str, Definition] = {}
        self._loaded = False            # a good set has been adopted at least once
        self._errors: list[str] = []
        self._configured = False
        self._tracks: dict[tuple, dict] = {}

    # ------------------------------------------------------------ loading
    def _configured_path(self):
        try:
            value = self._path_fn()
        except Exception:  # noqa: BLE001 - a broken config reader must not crash ingest
            return True, None, ["control_plane.deployment_definitions could not be read"]
        if value is None or value == "":
            return False, None, []
        if not isinstance(value, str) or not os.path.isabs(value):
            return True, None, ["control_plane.deployment_definitions must be an absolute directory path"]
        return True, value, []

    @staticmethod
    def _signature(path):
        """Directory and entry metadata (lstat: a swapped symlink or a chmod changes it)."""
        try:
            d = os.stat(path)
            names = sorted(n for n in os.listdir(path) if n.endswith((".yaml", ".yml")))
            stats = []
            for n in names[:MAX_FILES + 1]:
                st = os.lstat(os.path.join(path, n))
                stats.append((n, st.st_mode, st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino))
            return (path, d.st_mode, d.st_mtime_ns, d.st_ctime_ns, d.st_ino, tuple(stats))
        except OSError:
            return (path, None)

    def _refresh(self):
        """Re-read the directory when its signature moved. File I/O runs outside the lock;
        any failure is recorded as an error and keeps the last good set."""
        configured, path, errors = self._configured_path()
        sig = self._signature(path) if configured and not errors else None
        with self._lock:
            if not configured:
                self._configured, self._defs, self._loaded, self._errors, self._sig = False, {}, False, [], None
                return
            if not self._configured:            # newly configured: start from nothing
                self._defs, self._loaded = {}, False
            self._configured = True
            if errors:
                self._errors, self._sig = errors, None
                return
            if sig == self._sig:
                return
        try:
            defs, errors = load_definitions(path)
        except Exception as exc:  # noqa: BLE001 - never let a hostile file break ingest
            defs, errors = {}, [f"cannot load deployment definitions: {type(exc).__name__}"]
        with self._lock:
            if not self._configured:
                return
            self._sig = sig
            self._errors = errors
            if not errors:
                self._defs, self._loaded = defs, True

    def definitions(self) -> dict:
        self._refresh()
        with self._lock:
            return dict(self._defs)

    def status(self) -> dict:
        self._refresh()
        with self._lock:
            if not self._configured:
                warning = UNRESTRICTED_WARNING
            elif not self._loaded:
                warning = INVALID_WARNING
            elif self._errors:
                warning = STALE_WARNING
            else:
                warning = None
            return {"configured": self._configured, "loaded": self._loaded,
                    "errors": list(self._errors), "ingest_restricted": self._configured,
                    "warning": warning}

    # ----------------------------------------------------------- grouping
    def stages(self) -> dict:
        """{stage: [Definition sorted by (order, component)]}, stages ascending."""
        out: dict[int, list] = {}
        for d in _sorted(self.definitions().values()):
            out.setdefault(d.stage, []).append(d)
        return out

    def groups(self, defs=None) -> list:
        """[{stage, order, components}] in roll order (gateways before nodes)."""
        out = []
        for d in _sorted(defs if defs is not None else self.definitions().values()):
            if out and (out[-1]["stage"], out[-1]["order"]) == (d.stage, d.order):
                out[-1]["components"].append(d.component)
            else:
                out.append({"stage": d.stage, "order": d.order, "components": [d.component]})
        return out

    # -------------------------------------------------------- authorization
    def authorize(self, component, host) -> None:
        """Raise unless ``host`` (the authenticated fleet id) may report ``component``."""
        self._refresh()
        with self._lock:
            if not self._configured:
                return
            if not self._loaded:
                raise DeploymentDefinitionsUnavailable(INVALID_WARNING)
            d = self._defs.get(component)
        if d is None or d.host != host:
            raise DeploymentAuthorizationError(
                f"component {str(component)[:64]!r} is not defined for host {str(host)[:64]!r}")

    def annotate(self, snapshot: dict) -> dict:
        """The monitor snapshot plus ``definitions`` (status, groups) for the page."""
        view = copy.deepcopy(snapshot)
        view["definitions"] = {**self.status(), "groups": self.groups()}
        return view

    # ------------------------------------------------------- target + plan
    def _read_ci(self):
        """One read of the CI view (None when absent or unreadable)."""
        try:
            return self._ci_snapshot() if self._ci_snapshot is not None else None
        except Exception:  # noqa: BLE001 - an unreadable CI view keeps the last target
            return None

    def _green_rows(self, track, snap):
        repo, branch, ci_context = track
        event = _push_event(ci_context)
        if not isinstance(snap, dict) or not isinstance(snap.get("pipelines"), dict):
            return None
        rows = []
        for group in snap["pipelines"].values():
            if not isinstance(group, list):
                continue
            for row in group:
                if not isinstance(row, dict) or not isinstance(row.get("repository"), str):
                    continue
                if (row["repository"].lower() != repo.lower() or row.get("branch") != branch
                        or row.get("event") != event):
                    continue
                commit, run = row.get("revision") or row.get("commit"), row.get("run_number")
                if not isinstance(commit, str) or not commit or not _int(run):
                    continue
                rows.append((run, commit, row.get("state") or row.get("status")))
        return rows

    def _advance(self, track, snap):
        """Update one track's target from the CI view ``snap``; caller holds the lock."""
        st = self._tracks.setdefault(track, {"commit": None, "run": None, "rank": None, "generation": 0,
                                             "previous": None, "ranks": OrderedDict()})
        rows = self._green_rows(track, snap)
        if not rows:
            return st
        ranks = st["ranks"]
        # A commit's rank is the earliest run ever seen for it, so a rerun of an old
        # commit (a higher run number) does not make it newest.
        for run, commit, _state in rows:
            if commit not in ranks or run < ranks[commit]:
                ranks[commit] = run
            ranks.move_to_end(commit)           # seen now: the last to be forgotten
        while len(ranks) > RANK_MEMORY:      # forget the oldest, never the current target
            ranks.pop(next(c for c in ranks if c != st["commit"]))
        green = [(ranks.get(c, run), run, c) for run, c, state in rows if state == "success"]
        if not green:
            return st
        rank, run, commit = max(green)
        if st["commit"] is None or (commit != st["commit"] and rank > st["rank"]):
            if st["commit"] is not None:
                st["previous"] = st["commit"]
            st.update(commit=commit, run=run, rank=rank, generation=st["generation"] + 1)
        return st

    def _plan(self, track, defs, snap):
        st = self._advance(track, snap)
        return Plan(track=track, target_commit=st["commit"], run_number=st["run"],
                    generation=st["generation"], restarted=st["generation"] > 1,
                    previous_commit=st["previous"],
                    groups=self.groups([d for d in defs.values() if d.track == track]))

    def plans(self) -> dict:
        """{track: Plan} for every track a loaded definition follows. The CI view is read
        once for all tracks (the coordinator's one CI snapshot per tick)."""
        defs = self.definitions()
        tracks = sorted({d.track for d in defs.values()})
        if not tracks:
            return {}
        snap = self._read_ci()
        with self._lock:
            return {t: self._plan(t, defs, snap) for t in tracks}

    def plan(self, track=None):
        """The plan for ``track``, or for the only track; None when there is none."""
        plans = self.plans()
        if track is not None:
            return plans.get(tuple(track))
        if not plans:
            return None
        if len(plans) > 1:
            raise ValueError("several follows tracks are defined; name one")
        return next(iter(plans.values()))

    def target_for(self, component) -> Target | None:
        """The release ``component`` should run: its track's newest green commit, or None."""
        defs = self.definitions()
        d = defs.get(component)
        if d is None:
            return None
        snap = self._read_ci()
        with self._lock:
            plan = self._plan(d.track, defs, snap)
        if plan.target_commit is None:
            return None
        return Target(component=d.component, commit=plan.target_commit, run_number=plan.run_number,
                      repository=d.follows["repo"], branch=d.follows["branch"],
                      ci_context=d.follows["ci_context"], tag=d.follows["tag"],
                      asset=d.follows["asset"], generation=plan.generation)
