import test from 'node:test';
import assert from 'node:assert/strict';
import { createAttentionNotifier, itemId, planNotifications, quietNow } from '../attention-notify.js';
import { notifyLine, renderAttention } from '../attention-view.js';

const item = (over = {}) => ({ key: 'session:a', fingerprint: 'f1', kind: 'agent_failed', severity: 'critical', title: 'Amber Swift',
  detail: 'SECRET DETAIL', href: '#/herd/session/a', state: 'open', ...over });
const notify = (over = {}) => ({ browser: true, min_severity: 'critical', quiet_hours: { start: 22, end: 7 }, ...over });
const at = (hour) => new Date(2026, 9, 24, hour, 30);

function fakes({ permission = 'granted', stored = {} } = {}) {
  const shown = [];
  const store = new Map(Object.entries(stored));
  class FakeNotification {
    static permission = permission;
    static requested = 0;
    static async requestPermission() { FakeNotification.requested++; FakeNotification.permission = 'granted'; return 'granted'; }
    constructor(title, options) { this.title = title; this.options = options; shown.push(this); }
    close() { this.closed = true; }
  }
  const storage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
  return { shown, store, storage, FakeNotification };
}
const make = (f, extra = {}) => createAttentionNotifier({ storage: f.storage, NotificationImpl: f.FakeNotification, now: () => at(12), ...extra });

test('quiet hours wrap midnight, and equal edges mean never quiet', () => {
  const q = { start: 22, end: 7 };
  assert.equal(quietNow(q, at(23)), true);
  assert.equal(quietNow(q, at(3)), true);
  assert.equal(quietNow(q, at(7)), false);
  assert.equal(quietNow(q, at(12)), false);
  assert.equal(quietNow({ start: 9, end: 17 }, at(12)), true);
  assert.equal(quietNow({ start: 5, end: 5 }, at(5)), false);
  assert.equal(quietNow(null, at(5)), false);
});

test('only open items at or above the minimum severity that were not seen are planned', () => {
  const items = [item(), item({ key: 'w', severity: 'warning' }), item({ key: 'a', state: 'acknowledged' }), item({ key: 'old' })];
  const plan = planNotifications({ items, notify: notify(), seen: new Set([itemId(items[3])]), date: at(12) });
  assert.deepEqual(plan.notifications.map((i) => i.key), ['session:a']);
  const warn = planNotifications({ items, notify: notify({ min_severity: 'warning' }), seen: new Set(), date: at(12) });
  assert.deepEqual(warn.notifications.map((i) => i.key), ['session:a', 'w', 'old']);
});

test('quiet hours hold notifications back without marking them seen; the baseline marks without notifying', () => {
  const quiet = planNotifications({ items: [item()], notify: notify(), seen: new Set(), date: at(23) });
  assert.deepEqual(quiet, { notifications: [], seenAdd: [] });
  const base = planNotifications({ items: [item()], notify: notify(), seen: new Set(), date: at(12), baseline: true });
  assert.deepEqual(base, { notifications: [], seenAdd: [itemId(item())] });
});

test('one notification per (key, fingerprint); a changed fingerprint announces again; the payload is title and link only', () => {
  const f = fakes();
  const n = make(f);
  n.enable();
  assert.equal(n.handle({ items: [item()], notify: notify() }), 0);            // baseline: already open when we started
  assert.equal(n.handle({ items: [item(), item({ key: 'session:b', title: 'Second' })], notify: notify() }), 1);
  assert.equal(n.handle({ items: [item(), item({ key: 'session:b' })], notify: notify() }), 0);   // same pair, no repeat
  assert.equal(n.handle({ items: [item({ fingerprint: 'f2' })], notify: notify() }), 1);          // the condition changed
  assert.deepEqual(f.shown.map((s) => s.title), ['Second', 'Amber Swift']);
  for (const shown of f.shown) {
    assert.equal(shown.options.body, '');
    assert.deepEqual(shown.options.data, { href: '#/attention' });
    assert.doesNotMatch(JSON.stringify(shown.options), /SECRET DETAIL/);
  }
});

test('clicking a notification opens the attention page', async () => {
  const f = fakes(); const opened = [];
  const n = make(f, { onClick: (href) => opened.push(href) });
  await n.enable();
  n.handle({ items: [], notify: notify() });
  n.handle({ items: [item()], notify: notify() });
  f.shown[0].onclick();
  assert.deepEqual(opened, ['#/attention']);
  assert.equal(f.shown[0].closed, true);
});

test('another tab of this browser that already announced an item stops this one repeating it', async () => {
  const f = fakes();
  const a = make(f); const b = make(f);
  await a.enable(); await b.enable();
  a.handle({ items: [], notify: notify() }); b.handle({ items: [], notify: notify() });
  assert.equal(a.handle({ items: [item()], notify: notify() }), 1);
  assert.equal(b.handle({ items: [item()], notify: notify() }), 0);
});

test('more than three new items collapse into one notification with a count and no item text', async () => {
  const f = fakes();
  const n = make(f);
  await n.enable();
  n.handle({ items: [], notify: notify() });
  const many = ['a', 'b', 'c', 'd'].map((k) => item({ key: k, title: `T-${k}` }));
  assert.equal(n.handle({ items: many, notify: notify() }), 1);
  assert.deepEqual(f.shown.map((s) => s.title), ['4 new attention items']);
});

test('nothing is announced while off, blocked, or when the server turns browser notifications off', async () => {
  const off = fakes();
  const n = make(off);
  assert.equal(n.status(), 'off');
  n.handle({ items: [], notify: notify() }); n.handle({ items: [item()], notify: notify() });
  assert.equal(off.shown.length, 0);
  const blocked = fakes({ permission: 'denied' });
  const b = make(blocked);
  assert.equal(await b.enable(), 'blocked');
  assert.equal(blocked.FakeNotification.requested, 0);
  const server = fakes();
  const s = make(server);
  await s.enable();
  s.handle({ items: [], notify: notify() });
  s.handle({ items: [item()], notify: notify({ browser: false }) });
  assert.equal(server.shown.length, 0);
  assert.equal(s.status(), 'server-off');
  assert.equal(createAttentionNotifier({ storage: null, NotificationImpl: undefined }).status(), 'unsupported');
});

test('enabling asks the browser once, remembers the choice, and baselines again', async () => {
  const f = fakes({ permission: 'default' });
  const n = make(f);
  assert.equal(await n.enable(), 'on');
  assert.equal(f.FakeNotification.requested, 1);
  assert.equal(f.store.get('herdr.attention.notify'), 'on');
  assert.equal(make(f).status(), 'on');                       // a later page load keeps the choice
  n.handle({ items: [item()], notify: notify() });
  assert.equal(f.shown.length, 0);                              // already open when enabled: not announced
  n.disable();
  assert.equal(n.status(), 'off');
  assert.equal(n.active(), false);
});

test('the page shows the notification state, policy and the one control', () => {
  assert.match(notifyLine({ status: 'off', policy: notify() }), /data-notify-toggle="on">Enable notifications/);
  const on = notifyLine({ status: 'on', policy: notify() });
  assert.match(on, /new critical items, quiet 22:00–07:00 in this browser's time/);
  assert.match(on, /data-notify-toggle="off">Turn off/);
  assert.match(notifyLine({ status: 'on', policy: notify({ min_severity: 'warning' }) }), /new warning and worse items/);
  assert.match(notifyLine({ status: 'blocked', policy: notify() }), /blocked/);
  assert.equal(notifyLine({ status: 'server-off', policy: notify() }), '');
  assert.equal(notifyLine(null), '');
  const body = { items: [], counts: { open: 0 }, partial: false, unavailable: [], notify: notify() };
  assert.match(renderAttention({ body, notify: { status: 'off', policy: body.notify } }, Date.now()), /Enable notifications/);
});
