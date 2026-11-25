"""Secret masking in the request display is unchanged by #973: same rules, order and matches.

The baseline is a committed fixture, ``fixtures/assistant_hub_mask_baseline_62a2cea2.json``: what the policy module at
commit 62a2cea25b8d (the merge base of the fix) displays for each corpus command.  Regenerate it with
``python tests/control_plane/fixtures/gen_assistant_hub_mask_baseline.py``, which reads the module through
``git show 62a2cea25b8d:herdr_engineering/control_plane/assistant_hub/policy.py``.  This test reads only the JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from herdr_engineering.control_plane.assistant_hub import policy

BASELINE = json.loads((Path(__file__).parent / "fixtures" / "assistant_hub_mask_baseline_62a2cea2.json").read_text())
CASES = BASELINE["cases"]


def _plain(cmd: str) -> bool:
    """Printable ASCII with single spaces only: the display must equal the baseline's character for character."""
    return all(32 <= ord(c) < 127 for c in cmd) and "  " not in cmd


def test_the_corpus_is_big_enough():
    assert len(CASES) >= 40 and BASELINE["commit"] == "62a2cea25b8d"


@pytest.mark.parametrize("case", CASES, ids=[f"{i}-{c['command'][:30]}" for i, c in enumerate(CASES)])
def test_the_same_secrets_are_masked_as_at_the_baseline(case):
    ours = policy.scrub_display_full(case["command"])[0]
    for secret in case["secrets"]:                               # exactly the secrets the baseline hides are hidden here
        assert (secret in ours) == (secret in case["masked"]), (case["command"], secret)
    assert ours == policy.render_visible(case["masked"]), case["command"]   # nothing else differs: only blanks become tokens


@pytest.mark.parametrize("case", [c for c in CASES if _plain(c["command"])], ids=lambda c: c["command"][:40])
def test_plain_commands_display_exactly_as_at_the_baseline(case):
    assert policy.scrub_display(case["command"]) == case["display"]
