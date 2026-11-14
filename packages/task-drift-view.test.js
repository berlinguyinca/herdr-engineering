// H06 is rendering only, so this test reads the HTML the view returns — never a click, a timer, a fetch, and never an alignment figure it hopes to find.
import test from 'node:test';
import assert from 'node:assert/strict';
import { renderDriftComparison, relevanceOf, relevanceLists, UNKNOWN } from './task-drift-view.js';

const at = (day) => Date.UTC(2026, 10, day, 9, 30);   // fixed instants: the view reads no clock, so neither does this test
const classes = (html) => [...html.matchAll(/class="drift-action ([^"]*)"/g)].map((m) => m[1]);
const report = { objective: 'Convert the ANDI series and keep every frame',
  actions: [{ id: 'a1', at: at(1), summary: 'read the source manifest', relevant: true }, { id: 'a2', at: at(2), summary: 'posted a comment elsewhere', relevant: false }, { id: 'a3', at: at(3), summary: 'opened an editor' }],
  expected: 12, actual: 19, tolerance: 4, scope: { added: ['out/<b>series</b>"raw.ndjson'], removed: [] },
  model: { name: 'some-model', quantization: 'q4_k_m' }, uncertainty: { level: 'medium', text: 'two feeds could not be read' } };
test('a relevant and an unrelated action render with different classes, and one fixture re-rendered is byte-identical', () => {
  const once = renderDriftComparison(report);
  assert.equal(once, renderDriftComparison(report));   // byte-identical: no clock, no randomness, no order the report did not give
  assert.deepEqual(classes(once), ['drift-relevant', 'drift-unrelated', 'drift-unclassed']); assert.notEqual(classes(once)[0], classes(once)[1]);
  assert.match(once, /class="drift-action drift-relevant" data-drift-action data-relevance="relevant" data-action-id="a1"/);
  assert.match(once, /class="drift-action drift-unrelated" data-drift-action data-relevance="unrelated" data-action-id="a2"/);
  assert.equal((once.match(/drift-relevant"/g) || []).length, 1); });   // only the action the report classes relevant is drawn as relevant
test('relevance comes only from what the report states: an action it never classes renders "relevance unknown", never relevant', () => {
  const html = renderDriftComparison(report);
  assert.match(html, /data-relevance="unknown" data-action-id="a3"><b>a3<\/b>[\s\S]*?<em>relevance unknown<\/em>/);
  assert.match(html, /class="drift-action drift-unclassed"/); assert.doesNotMatch(html, /<em>relevance: unknown<\/em>/);
  const listed = renderDriftComparison({ actions: [{ id: 'b1' }, { id: 'b2' }, { id: 'b3' }], relevance: { relevant: ['b1'], unrelated: ['b2'] } });
  assert.deepEqual(classes(listed), ['drift-relevant', 'drift-unrelated', 'drift-unclassed']);   // the report's own list, and nothing else, decides
  assert.equal(relevanceOf({ id: 'b2' }, relevanceLists({ relevance: { unrelated: ['b2'] } })), 'unrelated');
  assert.equal(relevanceOf({ id: 'b1', relevant: false }), 'unrelated'); assert.equal(relevanceOf({ id: 'b9' }), UNKNOWN); assert.equal(relevanceOf(null), UNKNOWN); });
test('the drift verdict and its reason are H02 decideDrift, and the rendered HTML carries no percentage and no score token', () => {
  const html = renderDriftComparison(report).toLowerCase();
  assert.doesNotMatch(html, /%/); assert.doesNotMatch(html, /score/); assert.doesNotMatch(html, /alignment|similarity|match of/);
  assert.match(html, /data-drift-reason>actual is 7 away from expected, above the tolerance of 4</);   // H02's own words, unaltered
  const inside = renderDriftComparison({ expected: 12, actual: 13, tolerance: 4 });
  assert.match(inside, /<b>no drift<\/b><span>drift<\/span>/); assert.match(inside, /data-drift-reason>actual is within the tolerance of 4</);
  const bare = renderDriftComparison({});   // no numbers to compare: no verdict is manufactured here
  assert.match(bare, /<b>unknown<\/b><span>drift<\/span>/); assert.match(bare, /data-drift-reason>the report carries no expected, actual or tolerance, so drift is unknown</); });
test('the first violation is the record the report carries, and a decided drift with no such record says so rather than pointing at the first action', () => {
  const decided = renderDriftComparison({ expected: 1, actual: 9, tolerance: 0, actions: [{ id: 'a1', relevant: true }, { id: 'a2' }] });
  assert.match(decided, /data-drift-reason>actual is 8 away from expected, above the tolerance of 0</);   // drift is decided ...
  assert.match(decided, /data-drift-violation="unknown">first violation unknown[^<]*<\/p>/);               // ... and still no action is pointed at
  const carried = renderDriftComparison({ firstViolation: { actionId: 'a7', at: at(4) } });
  assert.match(carried, /<b>a7<\/b><span>first violation<\/span>/); assert.match(carried, /<b>2026-11-04 09:30<\/b><span>first violation at<\/span>/);
  assert.match(renderDriftComparison({ firstViolation: { actionId: 'a7' } }), /<b>unknown<\/b><span>first violation at<\/span>/); });   // an instant it does not carry is unknown
test('scope paths are escaped, an unlisted scope reads unknown and a scope listed empty reads none reported', () => {
  const html = renderDriftComparison(report);
  assert.match(html, /<code>out\/&lt;b&gt;series&lt;\/b&gt;&quot;raw\.ndjson<\/code>/);
  assert.ok(!html.includes('series</b>') && !html.includes('series"raw'));   // the markup and the quote never leave the escape
  assert.match(html, /data-drift-scope="removed"><b>removed<\/b><span class="drift-note">none reported<\/span>/);
  const bare = renderDriftComparison({});
  assert.match(bare, /data-drift-scope="added"><b>added<\/b><span class="drift-note">unknown<\/span>/);
  assert.match(bare, /data-drift-scope="removed"><b>removed<\/b><span class="drift-note">unknown<\/span>/); });
test('objective, model, quantization and uncertainty absent render the literal unknown, and the sections keep the plan order', () => {
  const bare = renderDriftComparison({});
  assert.match(bare, /data-drift-objective>unknown<\/p>/); assert.match(bare, /<b>unknown<\/b><span>model<\/span>/); assert.match(bare, /<b>unknown<\/b><span>quantization<\/span>/);
  assert.match(bare, /data-drift-uncertainty="unknown">uncertainty unknown[^<]*<\/p>/); assert.match(bare, /data-drift-empty>unknown<\/li>/); assert.doesNotMatch(bare, /data-drift-action/);
  const html = renderDriftComparison(report), where = (re) => html.search(re);
  assert.match(html, /<b>some-model<\/b><span>model<\/span><\/div><div class="diag-field"><b>q4_k_m<\/b><span>quantization<\/span>/);
  assert.match(html, /<b>medium<\/b><span>uncertainty<\/span><\/div><p class="drift-note" data-drift-uncertainty="stated">two feeds could not be read<\/p>/);
  assert.ok(where(/data-drift-objective/) < where(/class="drift-actions"/) && where(/class="drift-actions"/) < where(/data-drift-reason/));
  assert.ok(where(/data-drift-reason/) < where(/data-drift-scope="added"/) && where(/data-drift-scope="removed"/) < where(/data-drift-uncertainty/));
  const before = JSON.stringify(report); renderDriftComparison(report); assert.equal(JSON.stringify(report), before); });   // the report is read, not rewritten
