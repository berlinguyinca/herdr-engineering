"""The one handoff store every repo shares, and the one-off import of the per-repo stores it replaces."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

LEGACY_SUBDIR = Path(".herdr") / "handoffs"  # a repo's own store before the shared one; its records are under `outbox/`


def shared_store_root(env: Mapping[str, str]) -> Path:
    """The rule herdr-handoff 0.5.0 documents (`shared_store_root`), restated here because the controller is a separate
    package and does not import it: `HERDR_HANDOFF_STORE`, else `~/.local/share/herdr-handoff/store`; a leading `~` or
    `~/` is read from `env["HOME"]` when present; `~user` stays a literal path. (herdr-handoff itself ranks `--root`,
    `HERDR_HANDOFF_FILE` and `HERDR_HANDOFF_DIR` above this; the controller always passes `--root`, so they never apply.)"""
    configured = env.get("HERDR_HANDOFF_STORE")
    home = env.get("HOME") or str(Path.home())
    if configured:
        return Path(home, configured[2:]) if configured == "~" or configured.startswith("~/") else Path(configured)
    return Path(home) / ".local" / "share" / "herdr-handoff" / "store"


def import_legacy_args(store_root: Path, repo: Path) -> list[str]:
    """Arguments of `herdr-handoff store import-legacy` for one repo's old store; it copies what the shared store lacks
    and never deletes or changes the old one."""
    return ["--root", str(store_root), "store", "import-legacy", str(repo / LEGACY_SUBDIR)]
