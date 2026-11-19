"""Controller tunables."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ControllerConfig:
    drain_limit_pct: float = 80.0
    ceiling_margin_pts: float = 12.0
    drain_timeout_s: float = 1800.0
    briefing_timeout_s: float = 300.0
    max_attempts: int = 3

    @property
    def ceiling_pct(self) -> float:
        return self.drain_limit_pct + self.ceiling_margin_pts
