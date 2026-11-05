// H05 is rendering only, so this test asserts order, grouping and evidence links — never a click, a timer or a fetch.
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildTimeline } from './diagnostics-model.js';
import { groupBySession, stepOf, inputErrorOf, renderSessionTimeline, NO_SESSION } from './diagnostics-timeline.js';

const at = (day) => Date.UTC(2026, 10, day, 9, 30);   // fixed instants: the timeline reads no clock, so neither does this test
const ev = (id, sessionId, instant, extra = {}) => ({ id, missionKey: 'm1', at: instant, eventType: 'step_failed', sessionId, ...extra });
const timeline = (items) => buildTimeline(items).timeline;   // H02 is the order authority; this test only hands its output to the renderer
const rows = (html) => [...html.matchAll(/data-event="([^"]*)"/g)].map((m) => m[1]), sections = (html) => [...html.matchAll(/data-tl-session="([^"]*)"/g)].map((m) => m[1]);

test('events inside one session follow H02 chronologically, and a tie at one instant keeps H02 positional tiebreak', () => {
  const one = (items) => rows(renderSessionTimeline(timeline(items)));
  assert.deepEqual(one([ev('late', 's1', at(5)), ev('tie-z', 's1', at(3)), ev('early', 's1', at(1)), ev('tie-a', 's1', at(3))]), ['early', 'tie-z', 'tie-a', 'late']);
  assert.deepEqual(one([ev('tie-a', 's1', at(3)), ev('early', 's1', at(1)), ev('tie-z', 's1', at(3)), ev('late', 's1', at(5))]), ['early', 'tie-a', 'tie-z', 'late']);   // arrival order decides the tie, not the id
});

test('sessions are grouped by reported id, ordered by first instant then id, and two runs render byte-identical HTML', () => {
  const given = [ev('b1', 'beta', at(2)), ev('a2', 'alpha', at(7)), ev('a1', 'alpha', at(1)), ev('c1', 'crash', at(2)), ev('orphan', undefined, at(0))];
  const once = renderSessionTimeline(timeline(given));
  assert.equal(once, renderSessionTimeline(timeline([...given].reverse())));   // input order is not part of the answer
  assert.deepEqual(sections(once), [NO_SESSION, 'alpha', 'beta', 'crash']);
  assert.deepEqual(rows(once), ['orphan', 'a1', 'a2', 'b1', 'c1']);
  const groups = groupBySession(timeline(given));
  assert.deepEqual(groups.alpha.map((e) => e.id), ['a1', 'a2']);   // grouped lists keep H02's order
  assert.equal(groups[NO_SESSION][0].id, 'orphan'); assert.deepEqual(Object.keys(groupBySession([ev('p', '__proto__', at(1))])), ['__proto__']);   // an event with no sessionId is bucketed honestly, and "__proto__" stays a session id
});

test('a trace href carrying a quote or markup is escape-safe, and without one the trace says unavailable rather than linking to "#"', () => {
  const event = timeline([ev('e1', 's1', at(1))]), href = '/trace?s=1"><script>alert(1)</script>';
  const linked = renderSessionTimeline(event, { traceHref: href }); assert.equal((linked.match(/data-tl-trace="link"/g) || []).length, 1);
  assert.match(linked, /href="\/trace\?s=1&quot;&gt;&lt;script&gt;alert\(1\)&lt;\/script&gt;"/); assert.doesNotMatch(linked, /<script|<img|href="#"|href=""/);   // the url stays inside the attribute
  const alone = renderSessionTimeline(event);   // no href given
  assert.match(alone, /data-tl-trace="unavailable">No retry trace link was provided, so the retry trace for this session is unavailable<\/p>/);
  assert.doesNotMatch(alone, /<a |href=/);   // no dangling link, not even an empty one
});

test('an event carrying no step reads "unknown" — a step is never invented from position, id or order', () => {
  assert.equal(stepOf(ev('e', 's1', at(1))), 'unknown'); assert.equal(stepOf(null), 'unknown'); assert.equal(stepOf({ payload: {} }), 'unknown');
  assert.equal(stepOf({ payload: { step: 4 } }), '4'); assert.equal(stepOf({ payload: { step: ' build ' } }), 'build'); assert.equal(stepOf({ step: 0 }), '0');
  const html = renderSessionTimeline(timeline([ev('e1', 's1', at(1), { payload: {} })])); assert.match(html, /data-step="unknown"/); assert.match(html, />step unknown</); assert.doesNotMatch(html, /step [0-9]/);   // first row is not step one
});

test('a payload input error is labelled as one, and a section that could not be read is stated instead of an empty timeline', () => {
  const html = renderSessionTimeline(timeline([ev('e1', 's1', at(1), { payload: { inputError: '<img src=x onerror=alert(1)>' } }), ev('e2', 's1', at(2), { payload: {} })]));
  assert.equal((html.match(/data-input-error="true"/g) || []).length, 1); assert.match(html, /input error: &lt;img src=x onerror=alert\(1\)&gt;/);
  assert.equal(inputErrorOf({ payload: { input_error: 'no ANDI frame at this instant' } }), 'no ANDI frame at this instant'); assert.equal(inputErrorOf({ payload: { inputError: true } }), 'input error reported'); assert.equal(inputErrorOf({ payload: {} }), '');
  const down = renderSessionTimeline([], { unavailable: ['events'] });   // H03 named the section it could not read
  assert.match(down, /data-tl-unavailable/); assert.match(down, /events could not be read/); assert.doesNotMatch(down, /data-tl-empty|data-tl-session/);
  assert.match(renderSessionTimeline([]), /data-tl-empty/);
});

test('rendering reads the events it is handed and changes neither them nor their own order', () => {
  const items = [ev('e1', 's1', at(2), { payload: { step: 2 } }), ev('e2', undefined, at(1), { payload: { inputError: true } })], before = JSON.stringify(items);
  const html = renderSessionTimeline(items, { traceHref: '/trace' });   // rendered straight from the caller's array, not through buildTimeline
  assert.equal(JSON.stringify(items), before); assert.equal(items.length, 2); assert.match(html, /data-tl-session="unknown session"><h3>no session reported<\/h3>/);
});
