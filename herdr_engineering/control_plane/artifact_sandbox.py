"""Bounded subprocess boundary for renderer/build workers."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class SandboxError(ValueError):
    pass


@dataclass(frozen=True)
class SandboxResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool


class ArtifactSandbox:
    def __init__(self, *, allowed_executables: set[str], timeout_seconds: float = 60,
                 max_output_bytes: int = 256 * 1024):
        if not allowed_executables or timeout_seconds <= 0 or max_output_bytes < 1:
            raise ValueError("sandbox limits and allowlist are required")
        self.allowed_executables = frozenset(str(Path(item).resolve())
                                             for item in allowed_executables)
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def run(self, command: list[str], *, workdir: str | Path,
            env: dict[str, str] | None = None) -> SandboxResult:
        if not command:
            raise SandboxError("sandbox command is required")
        executable = str(Path(command[0]).resolve())
        if executable not in self.allowed_executables:
            raise SandboxError(f"executable is not allowlisted: {command[0]}")
        root = Path(workdir).resolve()
        if not root.is_dir():
            raise SandboxError("sandbox workdir must be an existing directory")
        clean_env = {"PATH": "/usr/bin:/bin", "HOME": str(root),
                     "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        if env:
            # Caller-supplied values are intentionally limited to non-secret
            # build flags; credentials and host process environment never pass.
            for key, value in env.items():
                if not key.startswith(("HERDR_RENDER_", "HERDR_BUILD_")):
                    continue
                clean_env[str(key)] = str(value)
        try:
            completed = subprocess.run(
                command, cwd=root, env=clean_env, text=True,
                capture_output=True,
                timeout=self.timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(None, self._clip(exc.stdout or b""),
                                 self._clip(exc.stderr or b""), True, True)
        stdout, stdout_truncated = self._clip_with_flag(completed.stdout)
        stderr, stderr_truncated = self._clip_with_flag(completed.stderr)
        return SandboxResult(completed.returncode, stdout, stderr, False,
                             stdout_truncated or stderr_truncated)

    def _clip(self, value) -> str:
        return self._clip_with_flag(value)[0]

    def _clip_with_flag(self, value) -> tuple[str, bool]:
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        encoded = str(value).encode("utf-8", "replace")
        if len(encoded) <= self.max_output_bytes:
            return str(value), False
        return encoded[:self.max_output_bytes].decode("utf-8", "ignore"), True

