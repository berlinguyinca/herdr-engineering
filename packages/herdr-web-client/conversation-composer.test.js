import test from 'node:test';
import assert from 'node:assert/strict';
import { completionAt } from '../conversation-composer.js';

test('completion supports agent handles, explicit paths, issues, sessions and commands', () => {
  for (const [text, query, trigger] of [['@coder', 'coder', '@'], ['Inspect @src/main.js', 'src/main.js', '@'], ['@org/repo#12', 'org/repo#12', '@'], ['@session:one', 'session:one', '@'], ['/help', 'help', '/']]) {
    assert.deepEqual(completionAt(text, text.length), { start: text.lastIndexOf(trigger), query, trigger });
  }
});

test('completion ignores addresses and closed mentions and respects caret position', () => {
  for (const text of ['hello@example.com', '@coder do this', 'text <script>']) assert.equal(completionAt(text, text.length), null);
  assert.deepEqual(completionAt('hi @cod suffix', 7), { start: 3, query: 'cod', trigger: '@' });
});
