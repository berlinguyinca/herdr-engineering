"""ansible-core 2.24 removes top-level fact injection (INJECT_FACTS_AS_VARS).
Facts must be read as ansible_facts['name']; any other ansible_* name must be
a connection/magic variable on the allowlist below."""

import re
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parent.parent / "ansible"

# Connection options, magic variables and registered-result helpers: not facts.
NOT_FACTS = {
    "ansible_facts", "ansible_check_mode", "ansible_diff_mode", "ansible_user", "ansible_host",
    "ansible_port", "ansible_connection", "ansible_python_interpreter", "ansible_become",
    "ansible_become_user", "ansible_become_method", "ansible_failed_result", "ansible_failed_task",
    "ansible_play_hosts_all", "ansible_play_hosts", "ansible_play_batch", "ansible_version",
    "ansible_loop", "ansible_loop_var", "ansible_search_path", "ansible_playbook_python",
    "ansible_inventory_sources", "ansible_run_tags", "ansible_skip_tags",
}


def _offenders():
    found = []
    for path in sorted(ANSIBLE.rglob("*")):
        if not path.is_file() or path.suffix not in {".yml", ".yaml", ".j2", ".tmpl"}:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for name in re.findall(r"\bansible_[a-z0-9_]+", line):
                if name not in NOT_FACTS:
                    found.append(f"{path.relative_to(ANSIBLE.parent)}:{n}: {name}")
    return found


def test_no_injected_top_level_facts():
    assert _offenders() == []
