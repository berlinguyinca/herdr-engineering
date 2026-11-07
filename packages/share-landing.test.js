import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {
  expiresText, landingBarModel, renderLandingBar, renderSigninCard, safeReturnPath, rememberReturn, takeReturn,
  RETURN_KEY, NOTICE,
} from '../herdr_engineering/control_plane/share_static/landing.js';

// A minimal element tree: just enough surface for the builders (createElement, textContent, appendChild, listeners).
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.className = ''; this.textContent = ''; }
  setAttribute(k, v) { this.attrs[k] = v; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  text() { return [this.textContent, ...this.children.map((c) => c.text())].filter(Boolean).join(' | '); }
  find(cls) { return this.className === cls ? this : this.children.map((c) => c.find(cls)).find(Boolean) ?? null; }
}
const doc = { createElement: (t) => new El(t) };
const DAY = 86400;
const NOW = 1_900_000_000;

test('expiry text counts whole days, today and singular', () => {
  assert.equal(expiresText(NOW + 30 * DAY - 5, NOW), 'expires in 29 days');
  assert.equal(expiresText(NOW + 3 * DAY, NOW), 'expires in 3 days');
  assert.equal(expiresText(NOW + 1.5 * DAY, NOW), 'expires in 1 day');
  assert.equal(expiresText(NOW + 3600, NOW), 'expires today');
  assert.equal(expiresText(NOW - 10, NOW), 'expires today');
  assert.equal(expiresText(null, NOW), '');
});

test('renderLandingBar shows sharer, title and time left', () => {
  const bar = renderLandingBar(doc, { sharer: 'Ada Lovelace', title: 'Q3 report', expiresAt: NOW + 30 * DAY - 5, now: NOW, signedIn: true });
  assert.equal(bar.find('herdr-landing-text').textContent, 'Shared by Ada Lovelace · Q3 report · expires in 29 days');
});

test('renderLandingBar expires today', () => {
  const bar = renderLandingBar(doc, { sharer: 'Ada', title: 't', expiresAt: NOW + 60, now: NOW, signedIn: true });
  assert.match(bar.find('herdr-landing-text').textContent, /expires today$/);
});

test('missing sharer shows "Shared by a lab member"', () => {
  for (const sharer of [undefined, null, '', '   ']) {
    assert.match(landingBarModel({ sharer, title: 'x', expiresAt: NOW + 2 * DAY, now: NOW }).text, /^Shared by a lab member/);
  }
});

test('signed out adds the notice and the button; signed in is the bar only', () => {
  let started = 0;
  const out = renderLandingBar(doc, { sharer: 'A', title: 't', expiresAt: NOW + DAY, now: NOW, signedIn: false }, () => { started += 1; });
  assert.equal(out.find('herdr-landing-notice').textContent, NOTICE);
  assert.equal(NOTICE, 'Sign in with your lab account to add this to your library. Signing in does not change who can see it.');
  const button = out.find('herdr-landing-signin');
  assert.equal(button.textContent, 'Sign in to keep this');
  button.listeners.click();
  assert.equal(started, 1);
  const inn = renderLandingBar(doc, { sharer: 'A', title: 't', expiresAt: NOW + DAY, now: NOW, signedIn: true });
  assert.equal(inn.find('herdr-landing-notice'), null);
  assert.equal(inn.find('herdr-landing-signin'), null);
});

test('the sign-in card has the heading, the line and one button', () => {
  let went = 0;
  const card = renderSigninCard(doc, () => { went += 1; });
  assert.equal(card.children[0].textContent, 'Sign in to Artifacts');
  assert.equal(card.children[1].textContent, 'Lab members: use your lab account. Opening a shared link doesn’t need an account.');
  assert.equal(card.children[2].textContent, 'Continue with lab sign-in');
  card.children[2].listeners.click();
  assert.equal(went, 1);
});

test('landing script makes no network request to record a claim', async () => {
  const source = fs.readFileSync(new URL('../herdr_engineering/control_plane/share_static/landing.js', import.meta.url), 'utf8');
  const code = source.split('\n').filter((l) => !l.trim().startsWith('*') && !l.trim().startsWith('/*') && !l.trim().startsWith('//')).join('\n');
  assert.doesNotMatch(code, /\b(fetch|XMLHttpRequest|sendBeacon|WebSocket|EventSource)\b/);
  assert.doesNotMatch(code, /api\/member\/claims/);
  const real = globalThis.fetch;
  globalThis.fetch = () => { throw new Error('network request'); };
  try {
    const bar = renderLandingBar(doc, { sharer: 'A', title: 't', expiresAt: NOW + DAY, now: NOW, signedIn: false }, () => {});
    bar.find('herdr-landing-signin').listeners.click();
    renderLandingBar(doc, { sharer: 'A', title: 't', expiresAt: NOW + DAY, now: NOW, signedIn: true });
  } finally {
    globalThis.fetch = real;
  }
});

const TOKEN = 'A'.repeat(43);

test('return path accepts only same-origin /s/ paths', () => {
  assert.equal(safeReturnPath(`/s/${TOKEN}/`), `/s/${TOKEN}/`);
  assert.equal(safeReturnPath(`/s/${TOKEN}/f/a%20b.html`), `/s/${TOKEN}/f/a%20b.html`);
  for (const bad of ['//evil.example/s/x', 'https://evil.example/s/x', '/\\evil.example', '/s/\\evil', '/s//evil.example/',
    '/evil', '/s', 's/x', '', null, 5, '/s/../evil', '/s/a/%2e%2e/b', '/s/a%2fb', '/s/a b', '/s/a\nb', '/s/a?x=1', '/s/a#h',
    '/s/callback', '/s/static/share.js', 'javascript:alert(1)', `/s/${'a'.repeat(600)}`]) {
    assert.equal(safeReturnPath(bad), null, JSON.stringify(bad));
  }
});

test('return path round-trips through sessionStorage and never a URL', () => {
  const map = new Map();
  const store = { getItem: (k) => (map.has(k) ? map.get(k) : null), setItem: (k, v) => map.set(k, v), removeItem: (k) => map.delete(k) };
  assert.equal(rememberReturn(store, 'https://evil.example/'), false);
  assert.equal(map.size, 0);
  assert.equal(rememberReturn(store, `/s/${TOKEN}/`), true);
  assert.equal(RETURN_KEY, 'herdr.share.return');
  assert.equal(map.get('herdr.share.return'), `/s/${TOKEN}/`);
  assert.equal(takeReturn(store), `/s/${TOKEN}/`);
  assert.equal(takeReturn(store), null);
  map.set('herdr.share.return', '//evil.example/');     // tampered storage is rejected on read
  assert.equal(takeReturn(store), null);
  assert.equal(takeReturn(null), null);
});

test('share.js keeps the return path out of every URL', () => {
  const source = fs.readFileSync(new URL('../herdr_engineering/control_plane/share_static/share.js', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /[?&]return=|searchParams\.set\(["']return|herdr\.share\.return/);
  assert.match(source, /rememberReturn\(store\(\), sharePath\)/);
});
