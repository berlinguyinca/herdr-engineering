#!/usr/bin/env python3
"""Host-side runner for sandboxed CI fix agents (one runner per host, a systemd timer).

The control plane only records a click (`POST /api/v1/ci/fix`). This runner, on a host listed in
`control_plane.ci_fix.hosts`, claims one request at a time over the operator API with the narrow `agent`
role, prepares an isolated copy of the failing commit, and reports every transition back. It is the only
holder of forge credentials: the sandbox it will start in a later step has none.

Each cycle (`once`):

  1. reads the validated settings the control plane serves (`GET /api/v1/ci/fix/config`); disabled, or this
     host not listed: do nothing;
  2. reconciles: a fix whose local record says it was still preparing (the previous run died) is failed and
     cleaned, a finished-but-unreported one is reported again, a fix the control plane thinks is active on
     this host but that has no local record is failed, and directories or mirror pins no record owns are removed;
  3. watches running fixes: cancel, deadline, agent quit -> finishing -> result gate -> final state;
  4. claims new work while this host has capacity, the launcher is available, disk is above the floor and a
     configured model is listed by the gateway (otherwise it says so and the request stays queued).

Preparing a fix: fetch the repository into a per-repository bare mirror with the runner's credential FILE
(read by a git credential helper, never put on a command line or in a log), pin the exact failing commit in the
mirror, make a standalone work copy under ~/herdr-agents/work/ci-fix/<id>/repo from the mirror with NO remote and
no credentials, check out the branch herdr-ci-fix/<run>-<sha7> at that commit. Cleanup always removes the work
directory and the mirror pin; the small state record stays until the final state has been reported.

Safety rails: every value reaching a command line is revalidated here and passed as an argument list (never a
shell string); git runs without the user's or the system's git config (a global `insteadOf` rewrite must not
change which credential is used); request text never becomes a command, a path or a URL beyond the validated
repository name on a configured forge host; the API key and the forge token are files read at use, mode 0600,
never in arguments, logs or the environment; nothing reads environment variables for configuration.

Usage: ci-fix-runner.py [once] --api-key-file FILE [--url URL] [--host-id ID] [--forge-token-file FILE]
                        [--forge-host HOST ...] [--gateway URL] [--state-dir DIR] [--work-root DIR]
                        [--mirror-root DIR] [--stub-agent]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

FIX_ID = re.compile(r"cifix_[0-9a-hjkmnp-tv-z]{26}\Z")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,119}\Z")
FORGE_HOST = re.compile(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]\Z")
REPO_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)+\Z")
BRANCH_PREFIX = "herdr-ci-fix/"
PIN_PREFIX = "refs/herdr-ci-fix/"
FETCH_TIMEOUT = 900.0
GIT_TIMEOUT = 300.0
CATALOGUE_MAX = 500
STATE_VERSION = 1
GIT_IDENTITY = ("herdr ci-fix agent", "ci-fix@herdr.invalid")


class Failure(Exception):
    """Something the runner can describe in one line; never carries a secret."""


class Rejected(Failure):
    """The control plane refused the call (a 4xx): retrying the same call will not help."""

    def __init__(self, status, message):
        super().__init__(f"control plane refused ({status}): {message}")
        self.status = status


def trim(value, limit=300):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ---------------------------------------------------------------------------- control plane
class HttpControlPlane:
    """The operator API, with the `agent` role key read from a file at use."""

    def __init__(self, url, key_file, timeout=30):
        self.url = url.rstrip("/")
        self.key_file = Path(key_file)
        self.timeout = timeout

    def _key(self):
        try:
            mode = self.key_file.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise Failure(f"{self.key_file} must not be readable by group or others")
            return self.key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise Failure(f"cannot read the API key file: {exc.strerror}") from None

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.url}/api/v1/ci/fix{path}", data=data, method=method,
            headers={"X-API-Key": self._key(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:    # noqa: S310
                return response.status, json.loads(response.read(4_000_000) or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read(100_000) or b"{}")
            except ValueError:
                payload = {}
            return exc.code, payload
        except (OSError, ValueError) as exc:
            raise Failure(f"control plane unavailable: {trim(exc, 120)}") from None

    def _ok(self, method, path, body=None):
        status, payload = self.call(method, path, body)
        if status >= 500:
            raise Failure(f"control plane error {status}")
        if status >= 400:
            raise Rejected(status, trim(payload.get("error", "")))
        return payload

    def config(self):
        return self._ok("GET", "/config")

    def claim(self, host, defer=None):
        return self._ok("POST", "/claim", {"host": host, **({"defer": defer} if defer else {})})

    def state(self, fix_id, state, **fields):
        return self._ok("POST", f"/{fix_id}/state", {"state": state, **fields})

    def result(self, fix_id, body):
        return self._ok("POST", f"/{fix_id}/result", body)

    def get(self, fix_id):
        status, payload = self.call("GET", f"/{fix_id}")
        if status == 404:
            return None
        if status >= 400:
            raise Failure(f"control plane error {status}")
        return payload

    def active(self, host):
        return self._ok("GET", f"?host={host}&active=1")["fixes"]


# ---------------------------------------------------------------------------- the LLM gateway
class Gateway:
    """The model catalogue (`GET /v1/models`), the same source the helper runner posts."""

    def __init__(self, url):
        self.url = (url or "").rstrip("/")

    def models(self):
        if not self.url:
            raise Failure("no gateway configured (--gateway or llm_gateway in host.json)")
        try:
            with urllib.request.urlopen(f"{self.url}/v1/models", timeout=15) as response:    # noqa: S310
                payload = json.loads(response.read(2_000_000))
        except (OSError, ValueError) as exc:
            raise Failure(f"gateway unreachable: {trim(exc, 120)}") from None
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise Failure("gateway returned no model list")
        return [row["id"] for row in rows[:CATALOGUE_MAX] if isinstance(row, dict) and isinstance(row.get("id"), str)]


# ---------------------------------------------------------------------------- the forge
class HttpsForge:
    """Where a repository is fetched from: https on a configured forge host, with the credential file."""

    def __init__(self, hosts=("github.com",), token_file=None):
        self.hosts = tuple(hosts)
        self.token_file = Path(token_file) if token_file else None

    def url(self, context):
        identity = context.get("repository_id")
        if not isinstance(identity, str) or "/" not in identity:
            raise Failure("the request has no repository identity")
        host, path = identity.split("/", 1)
        if host not in self.hosts or not FORGE_HOST.fullmatch(host):
            raise Failure(f"forge host {trim(host, 80)} is not configured on this runner")
        if not REPO_PATH.fullmatch(path) or ".." in path.split("/"):
            raise Failure("the repository name is not a plain path")
        return f"https://{host}/{path}.git"

    def credential_file(self):
        return self.token_file


# ---------------------------------------------------------------------------- git
class Git:
    """Plain git over argument lists, with no user or system config and no prompts."""

    def __init__(self, binary="git"):
        self.binary = binary

    def _env(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null", GIT_TERMINAL_PROMPT="0",
                   GIT_ASKPASS="/bin/false", LC_ALL="C")
        return env

    def run(self, args, *, cwd=None, timeout=GIT_TIMEOUT, credential_file=None, check=True):
        argv = [self.binary, "-c", "protocol.ext.allow=never", "-c", "submodule.recurse=false",
                "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
        if credential_file is not None:
            # a credential helper reads the token file when git asks; the token is never an argument
            helper = ("!f() { echo username=x-access-token; printf 'password=%s\\n' \"$(cat "
                      + shlex.quote(str(credential_file)) + ")\"; }; f")
            argv += ["-c", "credential.helper=", "-c", f"credential.helper={helper}"]
        argv += list(args)
        rest = list(args)
        name = (rest[2] if rest[:1] == ["-C"] and len(rest) > 2 else rest[0]) if rest else "git"
        try:
            done = subprocess.run(argv, cwd=cwd, env=self._env(), capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Failure(f"git {name}: {trim(exc, 120)}") from None
        if check and done.returncode != 0:
            raise Failure(f"git {name} failed: {trim(done.stderr or done.stdout, 200)}")
        return done

    def ensure_mirror(self, path):
        if not (path / "HEAD").exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.run(["init", "--bare", "-q", str(path)])

    def fetch(self, mirror, url, refspecs, credential_file=None):
        self.run(["-C", str(mirror), "fetch", "-q", "--no-tags", "--prune", url, *refspecs],
                 timeout=FETCH_TIMEOUT, credential_file=credential_file)

    def has_commit(self, mirror, sha):
        return self.run(["-C", str(mirror), "cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode == 0

    def pin(self, mirror, ref, sha):
        self.run(["-C", str(mirror), "update-ref", ref, sha])

    def unpin(self, mirror, ref):
        self.run(["-C", str(mirror), "update-ref", "-d", ref], check=False)

    def pins(self, mirror):
        done = self.run(["-C", str(mirror), "for-each-ref", "--format=%(refname)", PIN_PREFIX], check=False)
        return [line for line in done.stdout.splitlines() if line.startswith(PIN_PREFIX)]

    def size_bytes(self, path):
        total = 0
        for base, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.lstat(os.path.join(base, name)).st_size
                except OSError:
                    continue
        return total

    def make_work_clone(self, mirror, ref, dest, branch, sha):
        """A standalone copy at exactly ``sha`` on ``branch``: objects copied from the mirror, no remote, no alternates."""
        dest.mkdir(parents=True, mode=0o700)
        self.run(["init", "-q", str(dest)])
        self.run(["-C", str(dest), "fetch", "-q", "--no-tags", str(mirror), f"+{ref}:refs/heads/{branch}"])
        self.run(["-C", str(dest), "checkout", "-q", branch])
        self.run(["-C", str(dest), "config", "user.name", GIT_IDENTITY[0]])
        self.run(["-C", str(dest), "config", "user.email", GIT_IDENTITY[1]])
        self.run(["-C", str(dest), "config", "commit.gpgsign", "false"])
        if self.run(["-C", str(dest), "remote"], check=False).stdout.strip():
            raise Failure("the work copy unexpectedly has a remote")
        if self.head(dest) != sha:
            raise Failure("the work copy is not at the failing commit")

    def head(self, dest):
        return self.run(["-C", str(dest), "rev-parse", "HEAD"]).stdout.strip()

    def commits_since(self, dest, sha):
        out = self.run(["-C", str(dest), "rev-list", "--count", f"{sha}..HEAD"], check=False).stdout.strip()
        return int(out) if out.isdigit() else 0


# ---------------------------------------------------------------------------- the agent (stubbed here)
class NoLauncher:
    """Production default until the sandboxed Pi launch exists: nothing is claimed that cannot be run."""

    def available(self):
        return "this runner has no agent launcher yet"

    def start(self, fix):
        raise Failure("this runner has no agent launcher yet")

    def alive(self, fix):
        return False

    def stop(self, fix):
        return None


class StubLauncher:
    """Starts nothing; the agent 'quits' at once. For a dry run of the clone, state and cleanup plumbing."""

    def available(self):
        return None

    def start(self, fix):
        return {"stub": True, "session_id": None}

    def alive(self, fix):
        return False

    def stop(self, fix):
        return None


class CommitGate:
    """The result gate. Until the push and pull-request step exists it never pushes: no commit is `no_change`,
    anything else is refused with that reason."""

    def __init__(self, git):
        self.git = git

    def run(self, fix, config):
        commits = self.git.commits_since(Path(fix["repo"]), fix["revision"])
        if commits == 0:
            return "no_change", "the agent ended without a commit", None
        return "failed", "the result gate (push and pull request) is not available in this runner version; nothing was pushed", None


# ---------------------------------------------------------------------------- the runner
class Runner:
    def __init__(self, *, host_id, state_dir, work_root, mirror_root, control_plane, git, forge, gateway,
                 launcher, gate=None, clock=time.time, say=None):
        if not HOST.fullmatch(host_id):
            raise Failure("invalid host id")
        self.host_id = host_id
        self.state_dir = Path(state_dir)
        self.work_root = Path(work_root)
        self.mirror_root = Path(mirror_root)
        self.cp, self.git, self.forge, self.gateway, self.launcher = control_plane, git, forge, gateway, launcher
        self.gate = gate or CommitGate(git)
        self.clock = clock
        self.say = say or (lambda message: print(f"ci-fix-runner: {message}", file=sys.stderr))
        self.config = {}

    # ------------------------------------------------------------ local records
    def _fixes_dir(self):
        return self.state_dir / "fixes"

    def _record_path(self, fix_id):
        return self._fixes_dir() / fix_id / "state.json"

    def _work_dir(self, fix_id):
        return self.work_root / "ci-fix" / fix_id

    def save(self, fix):
        path = self._record_path(fix["id"])
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(fix, out, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def load_all(self):
        fixes, bad = [], []
        base = self._fixes_dir()
        for entry in sorted(base.iterdir()) if base.is_dir() else []:
            if not FIX_ID.fullmatch(entry.name):
                continue
            try:
                fix = json.loads((entry / "state.json").read_text(encoding="utf-8"))
                if fix.get("id") != entry.name or fix.get("version") != STATE_VERSION:
                    raise ValueError("record does not match its directory")
                fixes.append(fix)
            except (OSError, ValueError):
                bad.append(entry.name)
        return fixes, bad

    def _remove_tree(self, root, fix_id):
        """Remove ``root/<fix_id>`` only: a validated id under a fixed root, never through a symlink."""
        if not FIX_ID.fullmatch(fix_id):
            return
        target = root / fix_id
        if target.is_symlink():
            target.unlink(missing_ok=True)
            return

        def writable(function, path, _info):
            try:
                os.chmod(path, 0o700)
                function(path)
            except OSError:
                pass
        if target.exists():
            shutil.rmtree(target, onerror=writable)

    def release(self, fix):
        """Free everything a fix held on this host: the agent, the work copy, the mirror pin. Idempotent."""
        try:
            self.launcher.stop(fix)
        except Failure as exc:
            self.say(f"{fix['id']}: stopping the agent: {exc}")
        self._remove_tree(self.work_root / "ci-fix", fix["id"])
        if fix.get("mirror") and fix.get("pin") and not fix.get("pushed"):
            self.git.unpin(Path(fix["mirror"]), fix["pin"])

    def forget(self, fix_id):
        self._remove_tree(self._fixes_dir(), fix_id)

    # ------------------------------------------------------------ ending a fix
    def end(self, fix, state, reason=None, result=None):
        """Free the resources first, then report the final state; the record stays until it is reported."""
        self.release(fix)
        fix.update(phase="ended", final={"state": state, "reason": reason, "result": result})
        self.save(fix)
        self.report(fix)

    def report(self, fix):
        final = fix["final"]
        try:
            if final.get("result") is not None:
                self.cp.result(fix["id"], final["result"])
            self.cp.state(fix["id"], final["state"], **({"reason": final["reason"]} if final.get("reason") else {}))
        except Rejected as exc:
            self.say(f"{fix['id']}: {exc}; nothing more to report")      # e.g. the operator already cancelled it
        except Failure as exc:
            self.say(f"{fix['id']}: could not report {final['state']} yet ({exc}); will retry")
            return False
        self.forget(fix["id"])
        return True

    # ------------------------------------------------------------ one cycle
    def once(self):
        try:
            self.config = self.cp.config()
        except Rejected as exc:
            self.say(f"CI fix agents are not enabled here ({exc.status})")
            return
        if not self.config.get("enabled") or self.host_id not in self.config.get("hosts", []):
            self.say("CI fix agents are disabled, or this host is not listed; nothing to do")
            return
        self.reconcile()
        self.watch()
        self.start_new()

    # ------------------------------------------------------------ reconcile after a crash
    def reconcile(self):
        fixes, bad = self.load_all()
        for fix_id in bad:                                    # an unreadable record: free what it could hold
            self.say(f"{fix_id}: unreadable local record; cleaning up")
            self._remove_tree(self.work_root / "ci-fix", fix_id)
            self.forget(fix_id)
        known = {fix["id"] for fix in fixes}
        for fix in fixes:
            if fix["phase"] == "ended":
                self.report(fix)
            elif fix["phase"] in {"claimed", "preparing"}:    # this process is not preparing anything: it died
                self.end(fix, "failed", "the runner restarted while the fix was being prepared")
        try:
            active = self.cp.active(self.host_id)
        except Failure as exc:
            self.say(f"cannot list active fixes: {exc}")
            active = []
        for row in active:
            if row["id"] not in known and FIX_ID.fullmatch(row["id"]):
                try:
                    self.cp.state(row["id"], "failed", reason="the runner lost this fix (no local record)")
                except Failure as exc:
                    self.say(f"{row['id']}: {exc}")
        base = self.work_root / "ci-fix"
        for entry in sorted(base.iterdir()) if base.is_dir() else []:
            if entry.name not in known:
                self.say(f"{entry.name}: removing an orphaned work directory")
                self._remove_tree(base, entry.name)
        self.sweep_pins({fix["id"] for fix in fixes if fix["phase"] not in {"ended"}})

    def sweep_pins(self, live):
        mirrors = sorted(self.mirror_root.glob("*.git")) if self.mirror_root.is_dir() else []
        for mirror in mirrors:
            for ref in self.git.pins(mirror):
                if ref.removeprefix(PIN_PREFIX) not in live:
                    self.git.unpin(mirror, ref)

    # ------------------------------------------------------------ watch running fixes
    def watch(self):
        fixes, _ = self.load_all()
        for fix in fixes:
            if fix["phase"] != "running":
                continue
            try:
                view = self.cp.get(fix["id"])
            except Failure as exc:
                self.say(f"{fix['id']}: {exc}")
                continue
            if view is None or view["state"] in {"succeeded", "no_change", "failed", "cancelled", "expired"}:
                self.say(f"{fix['id']}: ended on the control plane; cleaning up")
                self.release(fix)
                self.forget(fix["id"])
            elif view.get("cancel_requested"):
                self.end(fix, "cancelled", "cancelled by the operator; stopped and cleaned up")
            elif self.clock() > fix["deadline"]:
                self.end(fix, "expired", f"deadline of {self.config['deadline_minutes']} minutes passed")
            elif not self.launcher.alive(fix):
                self.finish(fix)

    def finish(self, fix):
        """The agent quit: finishing, the result gate, the final state."""
        try:
            self.cp.state(fix["id"], "finishing")
        except Rejected as exc:
            self.say(f"{fix['id']}: {exc}")
        try:
            state, reason, result = self.gate.run(fix, self.config)
        except Failure as exc:
            state, reason, result = "failed", f"the result gate failed: {trim(exc, 200)}", None
        self.end(fix, state, reason, result)

    # ------------------------------------------------------------ claim and prepare
    def capacity(self):
        fixes, _ = self.load_all()
        return self.config["max_concurrent"]["per_host"] - sum(1 for fix in fixes if fix["phase"] != "ended")

    def blocked(self):
        """Why this host cannot start a fix right now, or None."""
        reason = self.launcher.available()
        if reason:
            return reason
        floor = self.config["limits"]["free_disk_gb"] * 1024 ** 3
        probe = self.work_root if self.work_root.exists() else self.work_root.parent
        probe.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(probe).free < floor:
            return f"less than {self.config['limits']['free_disk_gb']} GB free on this host"
        return None

    def choose_model(self):
        wanted = self.config.get("models") or []
        if not wanted:
            return None, "no model available: none configured in ci_fix.models"
        try:
            listed = set(self.gateway.models())
        except Failure as exc:
            return None, f"no model available: {exc}"
        for model in wanted:
            if model in listed:
                return model, None
        return None, "no model available: none of the configured models is listed by the gateway"

    def start_new(self):
        for _ in range(20):                                   # a bound, whatever the queue holds
            if self.capacity() <= 0:
                return
            why = self.blocked()
            if why:
                self.say(f"not claiming: {why}")
                return
            model, missing = self.choose_model()
            try:
                if model is None:
                    self.cp.claim(self.host_id, {"kind": "no_model", "reason": missing})
                    self.say(missing)
                    return
                claimed = self.cp.claim(self.host_id)
            except Failure as exc:
                self.say(f"claim: {exc}")
                return
            request = claimed.get("request")
            if not request:
                return
            self.prepare(request, model)

    def prepare(self, request, model):
        fix_id = request["id"]
        if not FIX_ID.fullmatch(fix_id):
            raise Failure("the control plane sent an invalid fix id")
        revision = request.get("revision", "")
        context = request.get("context") or {}
        fix = {"version": STATE_VERSION, "id": fix_id, "phase": "claimed", "host": self.host_id, "model": model,
               "revision": revision if isinstance(revision, str) else "", "number": request.get("number"),
               "repository": request.get("repository"), "created": self.clock(), "pushed": False}
        self.save(fix)
        try:
            if not SHA.fullmatch(fix["revision"]) or not isinstance(fix["number"], int) or fix["number"] < 1:
                raise Failure("the request has no exact commit or run number")
            if not MODEL.fullmatch(model):
                raise Failure("invalid model name")
            self.cp.state(fix_id, "preparing", model=model)
            fix["phase"] = "preparing"
            self.save(fix)
            fix.update(self.make_copy(fix, context))
            fix.update(phase="running", started=self.clock(),
                       deadline=self.clock() + self.config["deadline_minutes"] * 60)
            fix["agent"] = self.launcher.start(fix) or {}
            self.save(fix)
            fields = {"model": model, "branch": fix["branch"]}
            if fix["agent"].get("session_id"):
                fields["session_id"] = fix["agent"]["session_id"]
            self.cp.state(fix_id, "running", **fields)
        except Rejected as exc:
            self.say(f"{fix_id}: {exc}")
            self.release(fix)
            self.forget(fix_id)
        except Failure as exc:
            self.say(f"{fix_id}: failed: {exc}")
            self.end(fix, "failed", trim(exc, 400))

    def make_copy(self, fix, context):
        """Mirror fetch, pin the exact commit, standalone work copy on the fix branch."""
        sha, number = fix["revision"], fix["number"]
        url = self.forge.url(context)
        credential = self.forge.credential_file() if url.startswith("https://") else None
        limits = self.config["limits"]
        cap = limits["mirror_gb"] * 1024 ** 3
        mirror = self.mirror_root / (hashlib.sha256(context["repository_id"].encode()).hexdigest()[:20] + ".git")
        self.git.ensure_mirror(mirror)
        if self.git.size_bytes(mirror) > cap:
            raise Failure(f"the mirror of this repository is over {limits['mirror_gb']} GB")
        self.git.fetch(mirror, url, ["+refs/heads/*:refs/heads/*", "+refs/pull/*/head:refs/pull/*/head"], credential)
        if not self.git.has_commit(mirror, sha):
            try:
                self.git.fetch(mirror, url, [sha], credential)   # a commit no longer on any branch, by name
            except Failure:
                pass                                              # the check below says what matters
        if not self.git.has_commit(mirror, sha):
            raise Failure("the failing commit is not available from the repository after one fetch")
        if self.git.size_bytes(mirror) > cap:
            raise Failure(f"the mirror of this repository is over {limits['mirror_gb']} GB")
        pin = f"{PIN_PREFIX}{fix['id']}"
        self.git.pin(mirror, pin, sha)
        fix.update(mirror=str(mirror), pin=pin)
        self.save(fix)
        branch = f"{BRANCH_PREFIX}{number}-{sha[:7]}"
        work = self._work_dir(fix["id"])
        if work.exists():
            self._remove_tree(self.work_root / "ci-fix", fix["id"])
        self.git.make_work_clone(mirror, pin, work / "repo", branch, sha)
        return {"branch": branch, "work": str(work), "repo": str(work / "repo"), "mirror": str(mirror), "pin": pin}


# ---------------------------------------------------------------------------- command line
def host_json():
    path = Path.home() / ".config" / "herdr-engineering" / "host.json"
    try:
        data = json.loads(path.read_text())
        return str(data.get("control_plane_url") or ""), str(data.get("host_id") or ""), str(data.get("llm_gateway") or "")
    except (OSError, ValueError):
        return "", "", ""


def main(argv):
    url, host_id, gateway = host_json()
    home = Path.home()
    ap = argparse.ArgumentParser(prog="ci-fix-runner.py", description=__doc__.splitlines()[0])
    ap.add_argument("action", nargs="?", default="once", choices=["once"])
    ap.add_argument("--url", default=url, help="dashboard URL (default: control_plane_url from host.json)")
    ap.add_argument("--host-id", default=host_id or socket.gethostname().split(".")[0].lower())
    ap.add_argument("--api-key-file", required=True, help="file (mode 0600) holding an `agent` role API key")
    ap.add_argument("--forge-token-file", help="file (mode 0600) holding the forge token used to fetch (and, later, push)")
    ap.add_argument("--forge-host", action="append", default=None, help="forge host repositories may live on (default github.com)")
    ap.add_argument("--gateway", default=gateway, help="LLM gateway whose /v1/models lists the available models")
    ap.add_argument("--state-dir", default=str(home / ".local/state/herdr-engineering/ci-fix"))
    ap.add_argument("--work-root", default=str(home / "herdr-agents/work"))
    ap.add_argument("--mirror-root", default=str(home / "herdr-agents/ci-fix/mirrors"))
    ap.add_argument("--git", default="git")
    ap.add_argument("--stub-agent", action="store_true",
                    help="dry run: prepare the copy, let the 'agent' quit at once, clean up; nothing is pushed")
    args = ap.parse_args(argv)
    if not args.url or not HOST.fullmatch(args.host_id):
        ap.error("no dashboard URL (pass --url) or an invalid --host-id")
    git = Git(args.git)
    runner = Runner(
        host_id=args.host_id, state_dir=Path(args.state_dir), work_root=Path(args.work_root),
        mirror_root=Path(args.mirror_root), control_plane=HttpControlPlane(args.url, args.api_key_file), git=git,
        forge=HttpsForge(args.forge_host or ("github.com",), args.forge_token_file), gateway=Gateway(args.gateway),
        launcher=StubLauncher() if args.stub_agent else NoLauncher())
    try:
        runner.once()
    except Failure as exc:
        print(f"ci-fix-runner: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
