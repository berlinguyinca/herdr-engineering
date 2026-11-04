"""Verify exact Git quotes in operator-registered repositories, without inference."""
import asyncio
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


def _check(domain, item):
    root = domain.get('repository_path')
    content = item.get('content')
    if not isinstance(root, str) or not Path(root).is_dir() or not isinstance(content, str) or not content.strip():
        return False
    refs = item.get('metadata', {}).get('evidence', [])
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except ValueError:
            return False
    if not isinstance(refs, list) or not 1 <= len(refs) <= 16:
        return False
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
               GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1')
    for ref in refs:
        if not isinstance(ref, str) or len(ref) > 2048:
            return False
        match = re.fullmatch(r'git:([0-9a-f]{40}|[0-9a-f]{64}):([^\x00]+)', ref)
        if not match:
            return False
        sha, name = match.groups()
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts:
            return False
        spec = sha + ':' + name
        base = ['git', '--no-pager', '-C', root]
        try:
            size = subprocess.run(base + ['cat-file', '-s', spec], env=env,
                capture_output=True, timeout=3, check=True)
            if not 0 < int(size.stdout) <= 1024 * 1024:
                return False
            blob = subprocess.run(base + ['show', '--no-ext-diff', '--no-textconv', spec],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=3, check=True)
            if content not in blob.stdout.decode('utf-8'):
                return False
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            return False
    return True


async def verify_git_quote(domain, _actor, item):
    """Only an immutable exact quote is verified; request-supplied roots are ignored."""
    return await asyncio.to_thread(_check, domain, item)
