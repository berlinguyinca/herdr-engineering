"""Automatic handoff triggers: the session hands itself off before a summarizer does.

The operator requirement this module exists to satisfy is narrow and absolute: a handoff
is never something the human has to ask for. Waiting for the human is what the earlier
"proactive trigger" design did - it wrote prose into the next turn's context and left the
decision, and the typing, with the agent. Here the harness boundary that *knows* a session
is nearly out of context (Pi's `turn_end` + `ctx.getContextUsage()`, Claude Code's `Stop`
hook reading its own transcript) is observed by installed code, the verdict is decided by
policy in this module, and the agent is told to run the `handoff` skill itself.

Design notes worth knowing before editing:

* The trigger point is the harness boundary, not this program's guess. Pi compacts inside
  the same session (see `pi-coding-agent/dist/core/compact.js`), so compaction is not a
  place to hand over; `turn_end` with a full context is, because the next turn is the one
  that would be summarized.
* Policy lives here and nowhere else. The shipped Pi extension carries no thresholds: it
  asks `herdr-eng handoff autopilot check` and does what the answer says. That keeps the
  TypeScript honest and lets one config file govern every harness.
* `check` never consumes a trigger; only `record` does, and only after the instruction was
  actually injected. A crash between the two re-arms the trigger, which is the safe
  direction: a false "already handed off" record would silently strand the session.
* Triggers are recorded per session in `$XDG_STATE_HOME/herdr-engineering/handoff-autopilot.json`
  so the ledger is auditable, and the per-session cap stops a handoff loop.
* Explicit opt-out is real: `handoff_autopilot.enabled: false` in the machine config, or
  `HERDR_HANDOFF_AUTOPILOT=off` for a single session. The reason string names both.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # POSIX advisory locking; the ledger write stays unlocked where it does not exist.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from .config import declared_keys, load_config
from .errors import InvalidError

#: The percent of the context window at which a session is considered nearly out of
# context. The harness-neutral agent rule ("warn and recommend a fresh session past 50%")
# becomes an action here instead of a suggestion, so the same number is the default.
DEFAULT_CONTEXT_PERCENT = 50.0
DEFAULT_MAX_PER_SESSION = 1
DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000
# Assumed once usage proves the default window cannot be right. Neither harness reports its
# context window in the transcript, so the assumption has to yield to the evidence: a session
# that has used 250k tokens does not live in a 200k window. `handoff context-hook claude`
# applies the same rule; the autopilot must not measure a 1M session against 200k.
DEFAULT_LARGE_CONTEXT_WINDOW_TOKENS = 1_000_000
DEFAULT_HARNESSES = ("pi", "claude")

STATE_DIR_NAME = "herdr-engineering"
STATE_FILE_NAME = "handoff-autopilot.json"
#: The two state roots automatic handoffs are involved with, and the variable that moves each.
# They are different directories with different variables on purpose: the trigger ledger is
# freedesktop state under `$XDG_STATE_HOME`, while the opt-in `handoff context-hook` trigger keeps
# its per-session markers under `$HERDR_ENGINEERING_STATE`, this project's own state root. Setting
# one does not move the other - which is why `status` prints both, labelled (#934): the old output
# showed one unlabelled `state_path`, and an operator who sandboxed the wrong name still spent real
# triggers in the real ledger.
ENV_STATE_HOME = "XDG_STATE_HOME"
ENV_MARKER_HOME = "HERDR_ENGINEERING_STATE"
MARKER_DIR_NAME = "handoff-context"
DEFAULT_MARKER_STATE_ROOT_REL = (".local", "share", "herdr-engineering", "state")  # relative to $HOME
EXTENSION_FILE_NAME = "herdr-handoff-autopilot.ts"
PI_EXTENSION_DIR = ("pi", "agent", "extensions")  # ~/.pi/agent/extensions
CLAUDE_HOOK_EVENTS = ("Stop", "PreCompact")
#: Events whose hook output reaches the model in the installed Claude Code. Verified against
# the 2.1.306 binary: `hookSpecificOutput.additionalContext` is "non-error feedback
# delivered to the model; the conversation continues so the model can act on it" for Stop,
# SubagentStop, UserPromptSubmit, PostToolUse* - and PreCompact is excluded from that set.
CLAUDE_MODEL_REACHING_EVENTS = frozenset(
    {
        "Stop",
        "SubagentStop",
        "UserPromptSubmit",
        "SessionStart",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
    }
)

#: The envelope states that mean a handoff is already underway (herdr-handoff's own
# schemas/handoff-v2.schema.json enum; the rest are terminal).
ACTIVE_HANDOFF_STATES = frozenset({"draft", "queued", "claimed", "accepted", "in_progress"})
HANDOFF_SKILL = "handoff"
HANDOFF_CLI = "herdr-handoff"
ENV_OPT_OUT = "HERDR_HANDOFF_AUTOPILOT"
HOOK_MARKER = "handoff autopilot hook"
_FALSE = {"0", "off", "false", "no", "disable", "disabled", "never"}

TRIGGER_CODES = (
    "context_is_full",
    "below_threshold",
    "unknown_context",
    "disabled",
    "already_triggered",
    "handoff_underway",
    "harness_unsupported",
)


# ------------------------------------------------------------------- paths and policy


def _home(home: Path | str | None = None) -> Path:
    return Path(home) if home is not None else Path.home()


def _state_home(home: Path | str | None = None) -> Path:
    env = os.environ.get(ENV_STATE_HOME, "").strip()
    if env:
        return Path(env)
    return _home(home) / ".local" / "state"


def context_hook_marker_root(env: Mapping[str, str] | None = None) -> Path:
    """Where `handoff context-hook` keeps its per-session `<session>.fired` markers.

    This is the other state root, moved by `HERDR_ENGINEERING_STATE` and not by `XDG_STATE_HOME`.
    It lives here, beside the ledger's own accessor, so the two are read together and `status` can
    name them both; `handoff_context_hook._state_dir` resolves through this function rather than
    repeating the default, because two copies of a default path drift - and the drift is silent,
    since the autopilot would then read a directory the hook never wrote and warn a session again.
    """
    environ = os.environ if env is None else env
    root = environ.get(ENV_MARKER_HOME, "").strip()
    if not root:
        root = str(Path.home().joinpath(*DEFAULT_MARKER_STATE_ROOT_REL))
    return Path(root) / MARKER_DIR_NAME


def state_path(home: Path | str | None = None) -> Path:
    """Where automatic triggers are recorded (per user, auditable, mode 0600)."""
    return _state_home(home) / STATE_DIR_NAME / STATE_FILE_NAME


def pi_extension_dir(home: Path | str | None = None) -> Path:
    return _home(home) / ".pi" / "agent" / "extensions"


def pi_settings_path(home: Path | str | None = None) -> Path:
    return _home(home) / ".pi" / "agent" / "settings.json"


def extension_path(home: Path | str | None = None) -> Path:
    return pi_extension_dir(home) / EXTENSION_FILE_NAME


def claude_settings_path(home: Path | str | None = None) -> Path:
    return _home(home) / ".claude" / "settings.json"


def extension_source() -> str:
    """The shipped Pi extension, verbatim (installed unmodified so it stays reviewable)."""
    return (Path(__file__).resolve().parent / "extension_data" / EXTENSION_FILE_NAME).read_text()


@dataclass(frozen=True)
class Policy:
    enabled: bool = True
    context_percent: float = DEFAULT_CONTEXT_PERCENT
    max_per_session: int = DEFAULT_MAX_PER_SESSION
    harnesses: tuple[str, ...] = DEFAULT_HARNESSES
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    large_context_window_tokens: int = DEFAULT_LARGE_CONTEXT_WINDOW_TOKENS
    # True when the operator wrote context_window_tokens themselves. The default and a declared
    # value are otherwise indistinguishable, and they mean different things: one is a guess.
    window_declared: bool = False
    # Substring of a model name -> its context window, from `model_context_windows` in the YAML
    # config. The model is the one thing a Claude Code transcript does state, so this is how the
    # window is read from the session instead of guessed.
    model_windows: tuple[tuple[str, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "context_percent": self.context_percent,
            "max_per_session": self.max_per_session,
            "harnesses": list(self.harnesses),
            "context_window_tokens": self.context_window_tokens,
            "large_context_window_tokens": self.large_context_window_tokens,
            "window_declared": self.window_declared,
            "model_context_windows": dict(self.model_windows),
        }


def policy(config: dict[str, Any] | None = None) -> Policy:
    """Read `handoff_autopilot` from the machine config, falling back to safe defaults.

    A value of the wrong type is ignored rather than guessed at, except for
    `context_percent`, which is refused: silently defaulting a threshold would change when
    a session hands itself off, which is exactly the kind of surprise this feature cannot
    afford.
    """
    block = (config if config is not None else load_config()).get("handoff_autopilot") or {}
    enabled = block.get("enabled", True)
    context_percent = block.get("context_percent", DEFAULT_CONTEXT_PERCENT)
    if isinstance(context_percent, bool) or not isinstance(context_percent, (int, float)):
        raise InvalidError("handoff_autopilot.context_percent must be a number")
    if not 0 < float(context_percent) <= 100:
        raise InvalidError("handoff_autopilot.context_percent must be between 0 and 100")
    max_per_session = block.get("max_per_session", DEFAULT_MAX_PER_SESSION)
    if (
        isinstance(max_per_session, bool)
        or not isinstance(max_per_session, int)
        or max_per_session < 1
    ):
        raise InvalidError("handoff_autopilot.max_per_session must be a positive integer")
    harnesses = block.get("harnesses", list(DEFAULT_HARNESSES))
    if not isinstance(harnesses, (list, tuple)) or not all(isinstance(h, str) for h in harnesses):
        harnesses = list(DEFAULT_HARNESSES)
    window = block.get("context_window_tokens", DEFAULT_CONTEXT_WINDOW_TOKENS)
    if isinstance(window, bool) or not isinstance(window, int) or window < 1000:
        window = DEFAULT_CONTEXT_WINDOW_TOKENS
    large_window = block.get("large_context_window_tokens", DEFAULT_LARGE_CONTEXT_WINDOW_TOKENS)
    if isinstance(large_window, bool) or not isinstance(large_window, int) or large_window < 1000:
        large_window = DEFAULT_LARGE_CONTEXT_WINDOW_TOKENS
    # Usage beyond the configured window escalates to this one, so it can never be smaller than
    # the window it is supposed to replace.
    large_window = max(large_window, int(window))
    raw_models = block.get("model_context_windows", {})
    model_windows = tuple(
        (str(name).lower(), int(tokens))
        for name, tokens in (raw_models.items() if isinstance(raw_models, dict) else ())
        if name and isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 1000
    )
    return Policy(
        enabled=bool(enabled),
        context_percent=float(context_percent),
        max_per_session=int(max_per_session),
        harnesses=tuple(harnesses),
        context_window_tokens=int(window),
        large_context_window_tokens=int(large_window),
        model_windows=model_windows,
        # A merged config always contains the key, so declaredness has to come from the files.
        window_declared=(
            "context_window_tokens" in block
            if config is not None
            else "context_window_tokens" in declared_keys("handoff_autopilot")
        ),
    )


def model_window(model: str | None, windows: tuple[tuple[str, int], ...]) -> int | None:
    """The window the configured `model_context_windows` map gives `model`, longest match first."""
    if not model:
        return None
    name = model.lower()
    for pattern, tokens in sorted(windows, key=lambda item: -len(item[0])):
        if pattern in name:
            return tokens
    return None


def window_for(
    used_tokens: int,
    context_window: int,
    *,
    large_window: int = DEFAULT_LARGE_CONTEXT_WINDOW_TOKENS,
) -> int:
    """The window `used_tokens` should be measured against.

    A transcript says how much was used, never how much there was. Until usage contradicts the
    assumed window, the assumption stands; past it, the window is at least as large as the
    fallback, and never smaller than the usage itself - which keeps the percent inside 0-100
    instead of raising InvalidError and silently disabling the trigger for that session.
    """
    if used_tokens <= context_window:
        return max(1, int(context_window))
    return max(int(large_window), int(used_tokens), 1)


def opt_out_reason() -> str | None:
    """Why the autopilot is switched off, naming the exact way to switch it back."""
    value = os.environ.get(ENV_OPT_OUT, "").strip().lower()
    if value in _FALSE:
        return (
            f"{ENV_OPT_OUT}={value} switches off automatic handoffs for this session; "
            f"unset it to restore them"
        )
    return None


# ------------------------------------------------------------------------ the trigger


@dataclass(frozen=True)
class Decision:
    trigger: bool
    code: str
    reason: str
    percent: float | None = None
    prompt: str = ""
    threshold: float | None = None
    window: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "trigger": self.trigger,
            "code": self.code,
            "reason": self.reason,
            "percent": self.percent,
            "threshold": self.threshold,
            "prompt": self.prompt,
            "skill": HANDOFF_SKILL if self.trigger else None,
            "window": self.window,
        }


def prompt_text(
    *, percent: float, threshold: float, harness: str, boundary: str, handoff_cli: str = HANDOFF_CLI
) -> str:
    """The instruction injected into the session. It tells the agent to hand off itself."""
    return (
        f"[HerdR automatic handoff] Context is at {percent:.0f}% of the window "
        f"(threshold {threshold:.0f}%), noticed at the {boundary} boundary. This session is "
        "about to lose its detail to a summarizer, so hand off now, without asking me first - "
        "automatic handoffs are a standing operator instruction.\n"
        f"1. Use the `{HANDOFF_SKILL}` skill: it composes a self-sufficient briefing from the "
        "actual session, packages it, launches the successor pane in this same harness and "
        "proves the takeover before closing this one.\n"
        f"2. Or drive `{handoff_cli}` directly: create the handoff for this session, then "
        f"`{handoff_cli} launch <handoff_id>`.\n"
        "Do not ask for confirmation and do not finish the current task first; the summarizer "
        "runs before the next turn ends. Commit and push your work before the takeover. If the "
        "tool refuses to close this pane because work is uncommitted or unpushed, stop and "
        f"report the refusal - never pass --allow-dirty-source and never force the close. "
        f"Record this automatic trigger (context {percent:.0f}%, {boundary}) in the briefing's "
        "constraints so the successor knows the handoff was unprompted."
    )


def derive_percent(
    percent: float | None,
    tokens: int | None,
    context_window: int | None,
    *,
    default_window: int,
    large_window: int = DEFAULT_LARGE_CONTEXT_WINDOW_TOKENS,
) -> float | None:
    """Percent of the window in use, or None when the harness cannot say.

    Pi returns `percent: null` when `tokensUsed` or `contextWindow` is 0, so tokens are the
    fallback; a session that cannot report its size must never be handed off on a guess. A
    window the harness stated is used as stated - it knows its own size - but an assumed one
    yields to usage through `window_for`.
    """
    if percent is not None:
        return float(percent)
    if not tokens:
        return None
    if context_window:
        return float(tokens) / float(context_window) * 100.0
    window = window_for(int(tokens), int(default_window), large_window=large_window)
    return float(tokens) / float(window) * 100.0


def _window_report(
    active: Policy,
    *,
    percent: float | None,
    context_window: int | None,
    tokens: int | None,
    model: str | None = None,
) -> dict[str, Any]:
    """What the percentage was measured against, and how to stop guessing.

    Pi states its window; a Claude Code transcript does not, so the policy window is assumed
    until usage contradicts it. Declaring the real window is the only correct measurement on a
    big-window model, and every answer says which of the two happened - otherwise a handoff at a
    tenth of the real context reads like a threshold bug instead of a missing config line.
    """
    if percent is not None:
        # The harness computed the percentage itself; it knows its own window.
        return {"declared": True, "source": "harness"}
    from_model = None if context_window else model_window(model, active.model_windows)
    base = int(context_window or from_model or active.context_window_tokens)
    if context_window:
        source = "harness"
    elif from_model:
        source = "model"
    else:
        source = "config" if active.window_declared else "assumed"
    report: dict[str, Any] = {
        "declared": bool(context_window or from_model) or active.window_declared,
        "source": source,
        "window_tokens": base,
        # What the percentage was really divided by: usage beyond the window has to move it, and
        # an operator who declared 200k on a session using 250k should see both numbers.
        "measured_against_tokens": window_for(
            int(tokens or 0), base, large_window=int(active.large_context_window_tokens)
        ),
    }
    if source == "model":
        report["model"] = model
    if source == "assumed":
        report["how_to_declare"] = (
            "handoff_autopilot.context_window_tokens in ~/.config/herdr-engineering/config.yaml"
        )
    return report


def decide(
    *,
    session_id: str,
    harness: str,
    percent: float | None = None,
    tokens: int | None = None,
    context_window: int | None = None,
    model: str | None = None,
    cwd: Path | str | None = None,
    home: Path | str | None = None,
    config: dict[str, Any] | None = None,
    ledger: dict[str, Any] | None = None,
    boundary: str = "turn_end",
) -> Decision:
    """Whether this session should hand itself off now, and what to tell the agent."""
    try:
        active_policy = policy(config=config)
    except InvalidError as exc:
        return Decision(
            False,
            "disabled",
            f"automatic handoffs are off: {exc}; fix the "
            "handoff_autopilot block in the machine config",
        )
    threshold = active_policy.context_percent
    if percent is not None and not 0 <= float(percent) <= 100:
        raise InvalidError("percent must be between 0 and 100")
    reason = opt_out_reason()
    if reason is not None:
        return Decision(False, "disabled", f"automatic handoffs are off: {reason}")
    if not active_policy.enabled:
        return Decision(
            False,
            "disabled",
            "automatic handoffs are off: handoff_autopilot.enabled: false in the "
            "machine config (~/.config/herdr-engineering/config.yaml); set it to "
            f"true, or unset {ENV_OPT_OUT} if it is exported",
        )
    if harness not in active_policy.harnesses:
        return Decision(
            False,
            "harness_unsupported",
            f"{harness} has no installed automatic trigger yet; supported harnesses "
            f"are {', '.join(active_policy.harnesses)}",
        )
    # The harness's own statement wins; failing that, the window the session's model implies.
    stated_window = context_window or model_window(model, active_policy.model_windows)
    used = derive_percent(
        percent,
        tokens,
        stated_window,
        default_window=active_policy.context_window_tokens,
        large_window=active_policy.large_context_window_tokens,
    )
    if used is None:
        return Decision(
            False,
            "unknown_context",
            f"{harness} reported no usable context usage for {session_id}; a "
            "handoff is not triggered on an unknown context size",
        )
    window = _window_report(
        active_policy, percent=percent, context_window=context_window, tokens=tokens, model=model
    )
    if used < threshold:
        return Decision(
            False,
            "below_threshold",
            f"context at {used:.0f}% is below the {threshold:.0f}% handoff threshold",
            percent=used,
            threshold=threshold,
            window=window,
        )
    if _context_hook_warned(session_id):
        return Decision(
            False,
            "already_triggered",
            f"{session_id} was already told to hand off by `handoff context-hook claude` "
            "(the opt-in UserPromptSubmit trigger); this session will not be told twice",
        )
    entries = (ledger if ledger is not None else read_ledger(home=home)).get("sessions", {})
    count = int((entries.get(session_id) or {}).get("count", 0))
    if count >= active_policy.max_per_session:
        return Decision(
            False,
            "already_triggered",
            f"{session_id} was already told to hand off {count} time(s) this "
            f"session (handoff_autopilot.max_per_session: "
            f"{active_policy.max_per_session})",
        )
    underway = active_handoff(session_id, cwd=cwd)
    if underway is not None:
        return Decision(
            False,
            "handoff_underway",
            f"{underway['handoff_id']} is {underway['state']} for session "
            f"{session_id}; a handoff is already underway",
        )
    return Decision(
        True,
        "context_is_full",
        f"context at {used:.0f}% reached the {threshold:.0f}% threshold at the {boundary} boundary",
        percent=used,
        prompt=prompt_text(percent=used, threshold=threshold, harness=harness, boundary=boundary),
        threshold=threshold,
        window=window,
    )


# ------------------------------------------------------------------------ trigger ledger


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def read_ledger(home: Path | str | None = None) -> dict[str, Any]:
    ledger, _ = _read_ledger(home)
    return ledger


def _read_ledger(home: Path | str | None = None) -> tuple[dict[str, Any], str | None]:
    path = state_path(home)
    if not path.exists():
        return {"version": 1, "sessions": {}}, None
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"version": 1, "sessions": {}}, (
            f"trigger ledger {path} is unreadable; automatic triggers are still armed but the "
            "history could not be read"
        )
    if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), dict):
        return {"version": 1, "sessions": {}}, (
            f"trigger ledger {path} has no sessions mapping; automatic triggers are still "
            "armed but the history could not be read"
        )
    return {"version": int(raw.get("version", 1)), "sessions": raw["sessions"]}, None


def record_trigger(
    *,
    session_id: str,
    harness: str,
    percent: float | None = None,
    prompt: str = "",
    boundary: str = "turn_end",
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Record that this session was told to hand off. Returns the session's entry."""
    if not session_id:
        raise InvalidError("session_id is required to record an automatic handoff trigger")
    path = state_path(home)
    # The whole read-modify-write is inside the lock: releasing it before the write would leave
    # exactly the window this lock exists to close.
    with _ledger_lock(path):
        ledger, _ = _read_ledger(home)
        sessions = dict(ledger["sessions"])
        entry = dict(sessions.get(session_id) or {})
        count = int(entry.get("count", 0)) + 1
        now = _utc_now()
        entry.update(
            {
                "count": count,
                "first_at": entry.get("first_at", now),
                "last_at": now,
                "harness": harness,
                "last_percent": float(percent) if percent is not None else None,
                "last_boundary": boundary,
                "handoff": HANDOFF_CLI,
            }
        )
        if prompt:
            entry["last_prompt_sha256"] = _sha256(prompt)
        sessions[session_id] = entry
        _write_private_json(path, {"version": 1, "sessions": sessions})
    return entry


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def _atomic_write(path: Path, text: str, *, mode: int = 0o644) -> None:
    """Write a file so a crash cannot leave it half-written.

    Every file this module owns is read by another process at any moment - Pi loads the
    extension, Claude Code parses `settings.json` before each hook - so a partial write is a
    broken harness rather than a broken cache. The temp name is unique because a fixed `.tmp`
    name is shared by concurrent writers, and two sessions recording at once would otherwise
    overwrite each other's temporary file.

    `mode` applies only to a file that does not exist yet: an existing file keeps its own
    permissions (a 0600 `settings.json` holding secrets must not become world-readable), and a
    symlink is written through to its target rather than replaced by a regular file.
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _ledger_lock(path: Path, *, timeout: float = 5.0) -> Iterator[None]:
    """Best-effort exclusive lock over the ledger's read-modify-write.

    A Pi session and a Claude session can reach the threshold in the same second; without a
    lock, the second one reads before the first writes and its entry is lost, which re-arms
    that session's trigger. Failing to take the lock is not a reason to stop: losing an entry
    is the safe direction (the session is told again), while a hook that waits forever is the
    one outcome this module is not allowed to produce.
    """
    if fcntl is None:  # pragma: no cover - Windows
        yield
        return
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield
        return
    try:
        deadline = timeout
        waited = 0.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if waited >= deadline:
                    break
                time.sleep(0.02)
                waited += 0.02
        yield
    finally:
        os.close(descriptor)


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", mode=0o600)


# ------------------------------------------------------------------- other handoffs seen


def handoff_roots(cwd: Path | str | None = None, home: Path | str | None = None) -> list[Path]:
    """Directories where a handoff envelope for this session could be waiting."""
    start = Path(cwd) if cwd is not None else Path.cwd()
    roots: list[Path] = []
    for directory in [start, *start.parents]:
        candidate = directory / ".herdr" / "handoffs"
        if candidate.is_dir():
            roots.append(candidate)
    home_root = _home(home) / ".herdr" / "handoffs"
    if home_root.is_dir() and home_root not in roots:
        roots.append(home_root)
    return roots


def active_handoff(
    session_id: str, *, cwd: Path | str | None = None, home: Path | str | None = None
) -> dict[str, Any] | None:
    """The newest non-terminal handoff envelope naming this session, if one exists."""
    if not session_id:
        return None
    found: dict[str, Any] | None = None
    for root in handoff_roots(cwd=cwd, home=home):
        for envelope in sorted(root.glob("*.json")):
            try:
                package = json.loads(envelope.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(package, dict):
                continue
            source = package.get("source") or {}
            if source.get("session_id") != session_id:
                continue
            state = package.get("state")
            if state not in ACTIVE_HANDOFF_STATES:
                continue
            found = {
                "handoff_id": package.get("handoff_id") or envelope.stem,
                "state": state,
                "path": str(envelope),
            }
    return found


# --------------------------------------------------------------------- Claude Code hook


def transcript_tokens(path: Path | str) -> int | None:
    """Prompt-side tokens the last Claude Code assistant turn used, or None if unknowable.

    Each assistant record carries `message.usage` with `input_tokens`,
    `cache_creation_input_tokens` and `cache_read_input_tokens`; those three are the prompt side
    of the next request. The transcript is the only evidence Claude Code leaves, and it states
    usage but never the window size, so this returns tokens and the window is decided elsewhere.
    """
    path = Path(path)
    usage: dict[str, Any] | None = None
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            usage = message["usage"]
    if usage is None:
        return None
    return sum(
        int(usage.get(key, 0) or 0)
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )


def transcript_model(path: Path | str) -> str | None:
    """The model the last Claude Code assistant record names, or None.

    The transcript never states the window size but does name the model, and the model decides
    the window (a `[1m]` model has 1M). Both `message.model` and a record-level `model` are read.
    """
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return None
    model: str | None = None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        named = (message.get("model") if isinstance(message, dict) else None) or record.get("model")
        if isinstance(named, str) and named:
            model = named
    return model


def transcript_percent(
    path: Path | str,
    *,
    context_window: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
    large_window: int = DEFAULT_LARGE_CONTEXT_WINDOW_TOKENS,
) -> float | None:
    """Percent of the window the Claude Code transcript shows in use (tokens, then `window_for`)."""
    tokens = transcript_tokens(path)
    if tokens is None or not context_window:
        return None
    window = window_for(int(tokens), int(context_window), large_window=large_window)
    return tokens / float(window) * 100.0


def hook(
    harness: str, payload_text: str, *, record: bool = False, home: Path | str | None = None
) -> dict[str, Any]:
    """Handle a Claude Code hook event read from stdin; returns what to print.

    Fails open: unusable input yields an empty verdict and a zero exit, because a broken
    autopilot must never be able to stall the agent it is trying to help.
    """
    try:
        payload = json.loads(payload_text) if payload_text.strip() else {}
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return {}
    event = str(payload.get("hook_event_name") or "Stop")
    transcript = payload.get("transcript_path")
    tokens = transcript_tokens(transcript) if transcript else None
    try:
        decision = decide(
            session_id=session_id,
            harness=harness,
            tokens=tokens,
            model=transcript_model(transcript) if transcript else None,
            cwd=payload.get("cwd") or Path.cwd(),
            home=home,
            boundary=f"{harness} hook {event}",
        )
    except InvalidError as exc:
        return {"trigger": False, "code": "invalid", "reason": str(exc)}
    out = decision.to_dict()
    out["hook_event"] = event
    if not decision.trigger:
        return out
    if event in CLAUDE_MODEL_REACHING_EVENTS:
        out["hookSpecificOutput"] = {
            "hookEventName": event,
            "additionalContext": decision.prompt[:8000],
        }
        # Delivery is what spends the budget. An event that can reach the model but whose
        # record failed still counts as told (the instruction went out); an event that cannot
        # reach the model must leave the budget alone, or the default max_per_session of 1
        # would silence the next Stop with an instruction that was never delivered.
        if record:
            out["recorded"] = record_trigger(
                session_id=session_id,
                harness=harness,
                percent=decision.percent,
                prompt=decision.prompt,
                boundary=f"{harness} hook {event}",
                home=home,
            )
    else:
        # Verified against the installed Claude Code binary: PreCompact output does not
        # carry additionalContext, so the model cannot be reached here. Record and tell the
        # operator instead of pretending the instruction landed.
        out["systemMessage"] = (
            f"[herdr] context at {decision.percent:.0f}% - automatic handoff could not reach "
            f"the model on {event}; the next Stop hook will do it"
        )
        out["note"] = f"{event} hooks cannot deliver additionalContext in Claude Code"
        out["recorded"] = False
    return out


# ------------------------------------------------------------- install / uninstall / status


def _cli_args_override() -> list[str]:
    """Arguments placed after the CLI binary, from `HERDR_ENG_ARGS` (a JSON array of strings).

    `HERDR_ENG_BIN` names the executable and `HERDR_ENG_ARGS` the arguments after it - the same
    contract the shipped Pi extension reads, so a packager who sets them gets one command line
    in both harnesses. Nothing here is set by `scripts/install.sh`: production resolves the CLI
    from `PATH` (`herdr-eng`) or the extension's bundled `./bin/herdr-eng`. A malformed value is
    ignored rather than fatal, because this runs during install and a typo in an environment
    variable must not stop a machine from being installed.
    """
    raw = os.environ.get("HERDR_ENG_ARGS")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return [str(part) for part in parsed] if isinstance(parsed, list) else []


def _cli_command() -> str:
    """The stable command line the Claude hook runs, guarded against a missing CLI.

    Claude Code reports a non-zero hook exit to the session, so an uninstalled CLI or a moved
    venv would turn every Stop and PreCompact into a hook error (exit 127). The `[ -x ... ] &&
    ... || true` wrapper - the same shape the managed workflow hooks write - makes a missing
    binary silence instead. One fixed form so install is idempotent and `status` can compare it.
    """
    arguments = ["handoff", "autopilot", "hook", "--harness", "claude", "--record", "--json"]
    override = os.environ.get("HERDR_ENG_BIN", "").strip()
    prefix = _cli_args_override()
    if override:
        binary = override
    elif found := shutil.which("herdr-eng"):
        binary = found
    else:
        binary, prefix = sys.executable, ["-m", "herdr_engineering.cli"]
    # Every part is quoted, because a venv under a path with a space is legal and the result is
    # a line the operator's shell will execute.
    run = " ".join([shlex.quote(binary), *(shlex.quote(part) for part in prefix), *arguments])
    return f"[ -x {shlex.quote(binary)} ] && {run} || true"


def _context_hook_warned(session_id: str) -> bool:
    """Whether the opt-in `handoff context-hook claude` trigger already told this session.

    That trigger keeps its own per-session marker under `$HERDR_ENGINEERING_STATE/handoff-context`
    (default `~/.local/share/herdr-engineering/state`), outside this module's ledger. Without this
    check a session with both installed is told twice - once at half the window, once at the
    configured threshold - which is the nagging `max_per_session` exists to prevent. A marker root
    that is not a directory is read as "not warned": the decision must still be made.
    """
    root = context_hook_marker_root()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:128]
    try:
        return (root / f"{safe}.fired").is_file()
    except OSError:
        return False


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise InvalidError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError(f"{path} is not a JSON object")
    return data


def _is_our_hook(entry: Any) -> bool:
    return isinstance(entry, dict) and HOOK_MARKER in str(entry.get("command", ""))


def _has_our_hook(groups: Any) -> bool:
    """Whether a Claude Code hook group list already carries an entry we installed."""
    if not isinstance(groups, list):
        return False
    for group in groups:
        if not isinstance(group, dict):
            continue
        entries = group.get("hooks")
        if isinstance(entries, list) and any(_is_our_hook(entry) for entry in entries):
            return True
    return False


def merge_claude_hooks(
    settings: dict[str, Any],
    *,
    command: str | None = None,
    events: Iterable[str] = CLAUDE_HOOK_EVENTS,
) -> tuple[dict[str, Any], list[str]]:
    """Add our hooks to a Claude Code settings dict without touching anything else."""
    command = command or _cli_command()
    merged = json.loads(json.dumps(settings))
    hooks = merged.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    changed: list[str] = []
    for event in events:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
        if _has_our_hook(groups):
            continue
        groups = [*groups, {"matcher": "", "hooks": [{"type": "command", "command": command}]}]
        hooks[event] = groups
        changed.append(event)
    merged["hooks"] = hooks
    return merged, changed


def strip_claude_hooks(settings: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Remove only the hooks this installer added; foreign entries keep their place."""
    merged = json.loads(json.dumps(settings))
    hooks = merged.get("hooks")
    if not isinstance(hooks, dict):
        return merged, []
    changed: list[str] = []
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            entries = group.get("hooks")
            if not isinstance(entries, list):
                kept_groups.append(group)
                continue
            kept = [entry for entry in entries if not _is_our_hook(entry)]
            if len(kept) != len(entries):
                changed.append(event)
            if kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    merged["hooks"] = hooks
    return merged, changed


def _item(
    items: list[dict[str, Any]], kind: str, status: str, path: Path, detail: str = ""
) -> None:
    items.append({"kind": kind, "status": status, "path": str(path), "detail": detail})


def install(*, home: Path | str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Install the automatic triggers: the Pi extension, the Claude Code hooks, the state dir.

    Idempotent by content - a second run reports every item unchanged - and it never rewrites
    a settings file it cannot read.
    """
    base = _home(home)
    active_policy = policy()  # raises InvalidError before anything is written
    items: list[dict[str, Any]] = []
    state_dir = state_path(base).parent
    if dry_run:
        _item(items, "state-dir", "planned", state_dir, "trigger ledger lives here")
    elif state_dir.is_dir():
        _item(items, "state-dir", "unchanged", state_dir)
    else:
        state_dir.mkdir(parents=True, mode=0o700)
        _item(items, "state-dir", "created", state_dir)

    source = extension_source()
    target = extension_path(base)
    if dry_run:
        _item(items, "pi-extension", "planned", target, "fires at Pi's context boundary")
    elif target.exists() and target.read_text() == source:
        _item(items, "pi-extension", "unchanged", target)
    else:
        existed = target.exists()
        _atomic_write(target, source)
        _item(
            items,
            "pi-extension",
            "updated" if existed else "created",
            target,
            "fires at Pi's context boundary",
        )

    settings_file = claude_settings_path(base)
    settings = _read_json_object(settings_file)
    merged, changed = merge_claude_hooks(settings)
    if not changed:
        _item(items, "claude-hook", "unchanged", settings_file)
    elif dry_run:
        _item(
            items,
            "claude-hook",
            "planned",
            settings_file,
            "registers " + ", ".join(changed) + " hooks",
        )
    else:
        existed = settings_file.exists()
        _atomic_write(settings_file, json.dumps(merged, indent=2) + "\n", mode=0o600)
        _item(
            items,
            "claude-hook",
            "updated" if existed else "created",
            settings_file,
            "registered " + ", ".join(changed),
        )

    enabled = active_policy.enabled
    summary = (
        f"automatic handoffs are {'armed' if enabled else 'configured but disabled by policy'}: "
        f"Pi fires on turn_end, Claude Code on {'/'.join(CLAUDE_HOOK_EVENTS)}; the next "
        "full-context turn hands off without being asked"
    )
    return {
        "schema_version": 1,
        "ok": True,
        "dry_run": dry_run,
        "items": items,
        "policy": active_policy.to_dict(),
        "summary": summary,
    }


def uninstall(*, home: Path | str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Remove what `install` added. The trigger ledger is kept as the audit record."""
    base = _home(home)
    items: list[dict[str, Any]] = []
    target = extension_path(base)
    if dry_run:
        _item(items, "pi-extension", "planned", target, "would be removed")
    elif target.exists():
        target.unlink()
        _item(items, "pi-extension", "removed", target)
    else:
        _item(items, "pi-extension", "absent", target)

    settings_file = claude_settings_path(base)
    settings = _read_json_object(settings_file)
    merged, changed = strip_claude_hooks(settings)
    if not changed:
        _item(items, "claude-hook", "absent", settings_file)
    elif dry_run:
        _item(
            items,
            "claude-hook",
            "planned",
            settings_file,
            "would remove " + ", ".join(changed) + " hooks",
        )
    else:
        _atomic_write(settings_file, json.dumps(merged, indent=2) + "\n", mode=0o600)
        _item(items, "claude-hook", "removed", settings_file, "removed " + ", ".join(changed))
    return {
        "schema_version": 1,
        "ok": True,
        "dry_run": dry_run,
        "items": items,
        "summary": "automatic handoff triggers are no longer installed; the trigger ledger "
        f"at {state_path(base)} is kept",
    }


def installed(home: Path | str | None = None) -> dict[str, Any]:
    base = _home(home)
    target = extension_path(base)
    current = target.exists() and target.read_text() == extension_source()
    settings = _read_json_object(claude_settings_path(base))
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    claude = {event: _has_our_hook(hooks.get(event)) for event in CLAUDE_HOOK_EVENTS}
    return {
        "pi_extension": bool(current),
        "pi_extension_present": target.exists(),
        "pi_extension_path": str(target),
        "claude_hooks": claude,
        "claude_settings_path": str(claude_settings_path(base)),
        # Deprecated alias of `state_roots.trigger_ledger.path` in `status` output; kept for one
        # release because scripts read it (#934).
        "state_path": str(state_path(base)),
    }


def state_roots(home: Path | str | None = None) -> dict[str, Any]:
    """Every directory automatic handoffs write to, each labelled with the variable that moves it.

    Printing one path and calling it "the state root" is what made sandboxing a guess (#934): the
    ledger follows `XDG_STATE_HOME`, the context-hook markers follow `HERDR_ENGINEERING_STATE`, and
    only an explicit label tells a reader which to redirect.
    """
    return {
        "trigger_ledger": {"path": str(state_path(home)), "moves_with": ENV_STATE_HOME},
        "context_hook_markers": {"path": str(context_hook_marker_root()), "moves_with": ENV_MARKER_HOME},
    }


def status(*, home: Path | str | None = None) -> dict[str, Any]:
    active_policy = policy()
    ledger, note = _read_ledger(home)
    flags = os.environ.get(ENV_OPT_OUT, "").strip().lower()
    out = {
        "schema_version": 1,
        "policy": active_policy.to_dict(),
        "environment": {
            ENV_OPT_OUT: os.environ.get(ENV_OPT_OUT) or None,
            "opted_out": flags in _FALSE,
        },
        "installed": installed(home),
        "state_roots": state_roots(home),
        "ledger": {"path": str(state_path(home)), "sessions": ledger["sessions"]},
        "summary": (
            f"automatic handoffs are {'armed' if active_policy.enabled else 'disabled'} at "
            f"{active_policy.context_percent:.0f}% of the context window; the next boundary "
            "with a full context hands the session off without being asked. State roots are "
            f"listed under `state_roots`: the trigger ledger follows ${ENV_STATE_HOME} and the "
            f"context-hook markers follow ${ENV_MARKER_HOME}; one does not move the other"
        ),
    }
    if note:
        out["ledger"]["note"] = note
    return out


def doctor_check(*, home: Path | str | None = None) -> dict[str, Any]:
    """The `herdr-eng doctor` view: is the next handoff going to happen by itself?"""
    try:
        active_policy = policy()
    except InvalidError as exc:
        return {
            "name": "handoff-autopilot",
            "status": "fail",
            "detail": f"handoff_autopilot config is invalid: {exc}",
            "fix": "repair the handoff_autopilot block in the machine config",
            "data": {},
        }
    if not active_policy.enabled:
        return {
            "name": "handoff-autopilot",
            "status": "skip",
            "detail": "handoff_autopilot.enabled: false - handoffs will have to be asked for",
            "fix": "set handoff_autopilot.enabled: true in ~/.config/herdr-engineering/config.yaml",
            "data": active_policy.to_dict(),
        }
    flags = os.environ.get(ENV_OPT_OUT, "").strip().lower()
    details = installed(home)
    data = {**active_policy.to_dict(), **details, "state_path": details["state_path"]}
    problems: list[str] = []
    if flags in _FALSE:
        return {
            "name": "handoff-autopilot",
            "status": "skip",
            "detail": f"{ENV_OPT_OUT}={flags} switches off automatic handoffs for this shell",
            "fix": f"unset {ENV_OPT_OUT}",
            "data": data,
        }
    if "pi" in active_policy.harnesses and not details["pi_extension"]:
        problems.append(
            "the Pi extension is not installed"
            if not details["pi_extension_present"]
            else "the installed Pi extension is older than this build"
        )
    if "claude" in active_policy.harnesses and not any(details["claude_hooks"].values()):
        problems.append("no Claude Code hook is registered")
    if problems:
        return {
            "name": "handoff-autopilot",
            "status": "warn",
            "detail": "; ".join(problems),
            "fix": "run `herdr-eng handoff autopilot install`",
            "data": data,
        }
    ledger, note = _read_ledger(home)
    if note:
        return {
            "name": "handoff-autopilot",
            "status": "warn",
            "detail": note,
            "fix": f"remove or repair {state_path(home)}",
            "data": data,
        }
    return {
        "name": "handoff-autopilot",
        "status": "ok",
        "detail": (
            f"handoffs are automatic at {active_policy.context_percent:.0f}% of the "
            f"context window; {len(ledger['sessions'])} session(s) triggered so far"
        ),
        "fix": "",
        "data": data,
    }
