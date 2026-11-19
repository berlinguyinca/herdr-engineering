"""Controller tunables."""
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ControllerConfig:
    drain_limit_pct: float = 80.0
    ceiling_margin_pts: float = 12.0
    drain_timeout_s: float = 1800.0
    briefing_timeout_s: float = 300.0
    max_attempts: int = 3
    pane_gone_after_s: float = 60.0  # a pane with an open chain that Herdr does not list this long ends the chain `pane_gone`
    # Pushing the session's branch before a handoff. Default: never push (a repo's own rules may forbid it, and the
    # controller cannot read them); the briefing then records the unpushed commits. A worktree is allowed to push only
    # when it, or the main checkout of its repository, is one of `push_allowed_repos` (directories); any other
    # worktree follows `push_default`.
    push_allowed_repos: tuple[str, ...] = ()
    push_default: bool = False

    @property
    def ceiling_pct(self) -> float:
        return self.drain_limit_pct + self.ceiling_margin_pts


def config_from_env(env: Mapping[str, str]) -> ControllerConfig:
    """The defaults, with the push policy from `HERDR_HANDOFF_PUSH_REPOS` (directories separated by `:`) and
    `HERDR_HANDOFF_PUSH_DEFAULT=1`."""
    repos = tuple(part for part in env.get("HERDR_HANDOFF_PUSH_REPOS", "").split(":") if part)
    return ControllerConfig(push_allowed_repos=repos, push_default=env.get("HERDR_HANDOFF_PUSH_DEFAULT") == "1")
