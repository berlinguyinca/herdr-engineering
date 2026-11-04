import asyncio
import json
import subprocess

from herdr_engineering.control_plane.assistant_memory_evidence import verify_git_quote


def test_fact_evidence_must_match_an_immutable_quote_in_the_registered_repository(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    git('init', '-q')
    git('config', 'user.name', 'Memory test')
    git('config', 'user.email', 'memory-test@example.invalid')
    (root / 'policy.md').write_text('Release policy: use the blue slot before live traffic.\n')
    git('add', 'policy.md')
    git('commit', '-qm', 'policy')
    sha = git('rev-parse', 'HEAD')
    domain = {'repository_path': str(root)}
    item = {'content': 'use the blue slot before live traffic.',
            'metadata': {'evidence': json.dumps([f'git:{sha}:policy.md'])}}
    assert asyncio.run(verify_git_quote(domain, None, item))
    item['content'] = 'Use the red slot instead.'
    assert not asyncio.run(verify_git_quote(domain, None, item))
    item['content'] = 'use the blue slot before live traffic.'
    for reference in (f'git:{sha}:../policy.md', f'git:{sha}:/policy.md',
                      'git:HEAD:policy.md', 'unsupported:someone-said-so'):
        item['metadata']['evidence'] = [reference]
        assert not asyncio.run(verify_git_quote(domain, None, item))
    item['metadata']['evidence'] = [f'git:{sha}:policy.md']
    assert not asyncio.run(verify_git_quote({}, None, item))


def test_evidence_cannot_supply_another_repository_path(tmp_path):
    item = {'content': 'claim', 'metadata': {'repository_path': str(tmp_path),
        'evidence': ['git:' + 'a' * 40 + ':policy.md']}}
    assert not asyncio.run(verify_git_quote({}, None, item))
