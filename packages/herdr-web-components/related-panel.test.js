import test from 'node:test';
import assert from 'node:assert/strict';
import { renderRelated } from '../related-panel.js';

const answer = (over = {}) => ({ kind: 'mission', id: 'm', partial: [], more: {}, relations: {}, ...over });

test('empty groups are omitted and an empty answer says nothing is linked', () => {
  const html = renderRelated(answer({ relations: { agents: [{ id: 's1', label: 'Amber', state: 'active' }], ci: [] } }));
  assert.match(html, /Agents/);
  assert.doesNotMatch(html, /CI runs/);
  assert.match(renderRelated(answer()), /Nothing else is linked/);
});

test('loading, error and partial states are distinct', () => {
  assert.match(renderRelated(null), /Loading related items/);
  assert.match(renderRelated(null, { error: 'x' }), /could not be loaded/);
  const html = renderRelated(answer({ partial: ['ci', 'llm'], relations: { host: [{ id: 'b', label: 'b' }] } }));
  assert.match(html, /Could not read CI, LLM usage/);
});

test('links come from known kinds, ids are encoded and labels escaped', () => {
  const html = renderRelated(answer({ relations: { agents: [{ id: 'herdr:b:e:p', label: '<b>x</b>' }],
    ci: [{ id: '7:12', label: 'org/app · run #12', run_number: 12, repo_filter: 'o/r&x' }],
    pr: [{ id: 'p', label: 'org/app #1', repository: 'org/app', number: 1 }, { id: 'q', label: 'bad', repository: 'javascript:alert(1)', number: 1 }] } }));
  assert.match(html, /href="#\/herd\/session\/herdr%3Ab%3Ae%3Ap"/);
  assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/);
  assert.match(html, /href="#\/ci\?search=12&amp;repository=o%2Fr%26x"/);
  assert.match(html, /href="https:\/\/github.com\/org\/app\/pull\/1" target="_blank" rel="noopener noreferrer"/);
  assert.doesNotMatch(html, /javascript:/);
});

test('more counts and the llm summary render; a session links to its LLM usage page', () => {
  const html = renderRelated(answer({ more: { agents: 5 }, relations: { agents: [{ id: 'a', label: 'A' }], llm: { requests: 3, tokens_out: 9, problems: 1 } } }), { sessionId: 'herdr:b:e:p' });
  assert.match(html, /\+5 more/);
  assert.match(html, /href="#\/llm\/session\/herdr%3Ab%3Ae%3Ap">3 requests · 9 output tokens · 1 problems/);
  assert.doesNotMatch(renderRelated(answer({ relations: { llm: { requests: 1, tokens_out: 1, problems: 0 } } })), /href="#\/llm/);
});
