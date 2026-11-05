import test from 'node:test';
import assert from 'node:assert/strict';
import { escapeHtml, issueView, compareIssues, renderIssueList } from './diagnostics-view.js';

const at = (day) => Date.UTC(2026, 10, day, 9, 30);   // fixed instants: the view reads no clock, so neither does this test
const cards = (html) => [...html.matchAll(/data-diag-card[^>]*><h3>([^<]*)</g)].map((m) => m[1]);
const inc = (id, severity, day, extra = {}) => ({ incident: { id, kind: 'crash', severity, message: 'boom', at: at(day) }, ...extra });

test('escapeHtml covers the five characters, and a payload that looks like markup stays inert', () => {
  assert.equal(escapeHtml('&<>"\''), '&amp;&lt;&gt;&quot;&#39;');
  const html = renderIssueList([{ incident: { id: '" onmouseover="x', kind: 'crash', severity: 'error', message: '<script>alert(1)</script>' },
    occurrences: 2, sessions: ['a'], lastSeen: at(6), recoveryState: 'resolved<img src=x onerror=alert(1)>' }]);
  assert.doesNotMatch(html, /<script|<img|onmouseover="x/i);   // no live tag, and the quote cannot leave the attribute
  assert.match(html, /&quot; onmouseover=&quot;x/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /resolved&lt;img src=x onerror=alert\(1\)&gt;/);   // the payload survives as text, never as markup
});

test('cards sort by severity, then last seen descending, then fingerprint; tied records keep one reproducible order', () => {
  const rows = [inc('tie-z', 'critical', 3), inc('warn', 'warning', 9), inc('tie-a', 'critical', 3), inc('older-error', 'error', 1),
    inc('tie-m', 'critical', 3), inc('info', 'info', 9), inc('newer-error', 'error', 5), { id: 'undated', kind: 'k', severity: 'critical', message: 'm' }];
  const want = ['tie-a', 'tie-m', 'tie-z', 'undated', 'newer-error', 'older-error', 'warn', 'info'];
  assert.deepEqual(cards(renderIssueList(rows)), want);
  assert.deepEqual(cards(renderIssueList([...rows].reverse())), want);   // input order is not part of the answer
  assert.deepEqual([...rows].map(issueView).sort(compareIssues).map((v) => v.fingerprint), want);
});

test('a record H02 refuses is a visible rejected record — never a half card, never silence', () => {
  const html = renderIssueList([{ id: 'x', kind: 'crash', severity: '<script>alert(1)</script>', message: 'm', at: at(1) }, null, { id: '', kind: 'k', severity: 'info' }]);
  assert.equal((html.match(/data-diag-card/g) || []).length, 0);
  assert.equal((html.match(/data-diag-rejected/g) || []).length, 3);
  assert.match(html, /rejected record x: severity must be one of info\|warning\|error\|critical/);
  assert.match(html, /rejected record unknown: incident must be a plain object/);
  assert.doesNotMatch(html, /<script/);
});

test('every card figure comes from the record: reported where reported, unknown where not, and never "resolved"', () => {
  const bare = renderIssueList([{ id: 'fp', kind: 'k', severity: 'warning', message: 'm', sessionId: 's1', at: at(2) }]);
  assert.match(bare, /<b>unknown<\/b><span>recovery state<\/span>/);
  assert.match(bare, /<b>unknown<\/b><span>occurrences<\/span>/);
  assert.match(bare, /<b>1<\/b><span>sessions<\/span>/);   // its one reported session id, not an invented count
  assert.match(bare, /<b>2026-11-02 09:30<\/b><span>last seen<\/span>/);
  assert.doesNotMatch(bare, /resolved|<b>0<\/b>/);
  const reported = renderIssueList([{ incident: { id: 'fp', kind: 'k', severity: 'error', message: 'm' }, occurrences: 12, sessions: ['a', 'a', 'b'], lastSeen: at(4), recoveryState: 'open' }]);
  assert.match(reported, /<b>12<\/b><span>occurrences<\/span>/);
  assert.match(reported, /<b>2<\/b><span>sessions<\/span>/);   // distinct session ids, not the raw length
  assert.match(reported, /<b>2026-11-04 09:30<\/b><span>last seen<\/span>/);
  assert.match(reported, /data-recovery="open"/);
});

test('an unavailable section says unavailable instead of an empty list that looks like no issues', () => {
  const down = renderIssueList([], { state: { ok: true, partial: true, unavailable: ['events'] } });
  assert.match(down, /data-diag-unavailable/);
  assert.match(down, /events could not be read/);
  assert.doesNotMatch(down, /data-diag-(empty|partial)/);
  assert.match(renderIssueList([], { state: { ok: true, partial: false, unavailable: [] } }), /data-diag-empty/);
  assert.match(renderIssueList([], { state: { ok: true, partial: true, unavailable: [] } }), /data-diag-partial/);
  assert.doesNotThrow(() => renderIssueList(null));
  assert.deepEqual(cards(renderIssueList('no')), []);
});
