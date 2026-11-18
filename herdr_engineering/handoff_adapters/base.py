"""Shared types of the per-harness handoff adapters."""
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

MAX_TYPED_BYTES = 900  # typed into a terminal pane; longer input is mangled by paste handling

WindowSource = Literal["recorded", "model_table", "fallback"]


class Harness(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    PI = "pi"
    OPENCODE = "opencode"


@dataclass(frozen=True)
class SessionRef:
    harness: Harness
    pane_id: str
    cwd: Path
    session_id: str | None
    record_path: Path | None  # the transcript, rollout or db the adapter reads


@dataclass(frozen=True)
class ContextReading:
    used_tokens: int
    window_tokens: int
    source: str
    window_source: WindowSource
    measured_at: float

    @property
    def percent(self) -> float:
        return self.used_tokens / self.window_tokens * 100


@dataclass(frozen=True)
class ExitPlan:
    text: str | None
    keys: tuple[str, ...]
    process_end_timeout_s: float


@dataclass(frozen=True)
class TypedInput:
    text: str
    submit_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.text.encode()) > MAX_TYPED_BYTES:
            raise ValueError(f"typed input exceeds {MAX_TYPED_BYTES} UTF-8 bytes")


class HarnessAdapter(Protocol):
    harness: Harness

    def locate(self, pane_id: str, cwd: Path) -> SessionRef | None: ...

    def read_context(self, ref: SessionRef) -> ContextReading | None: ...

    def drain_input(self, order: str) -> TypedInput: ...

    def is_ready_for_input(self, screen: str, agent_status: str) -> bool: ...

    def exit_plan(self) -> ExitPlan: ...

    def compact_input(self) -> TypedInput: ...
