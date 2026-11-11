import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

// share.js is now an ES module importing lab-signin.js; vm runs classic scripts, so the two real sources are joined with the
// import line and the `export` keywords removed (nothing else is changed).
const STATIC = new URL('../herdr_engineering/control_plane/share_static/', import.meta.url);
const SOURCE = [
  fs.readFileSync(new URL('lab-signin.js', STATIC), 'utf8').replace(/^export /gm, ''),
  fs.readFileSync(new URL('landing.js', STATIC), 'utf8').replace(/^export /gm, ''),
  fs.readFileSync(new URL('share.js', STATIC), 'utf8').replace(/^import .*\.js";\s*$/gm, ''),
].join('\n');
const TOKEN = 'A'.repeat(43);
const OTHER = 'B'.repeat(43);
const SHARE = `/s/${TOKEN}/`;

// Runs the real share.js in a vm against a minimal browser surface; returns every location.replace/assign target.
async function run(landing) {
  const navigations = [];
  const store = new Map([['herdr-share-pkce', JSON.stringify({ state: 'st', verifier: 'v', share: SHARE })]]);
  const json = (body) => ({ ok: true, json: async () => body });
  const window = {
    sessionStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v),
      removeItem: (k) => store.delete(k),
    },
    crypto: webcrypto,
  };
  const context = vm.createContext({
    window, crypto: webcrypto, URLSearchParams, TextEncoder, Uint8Array, btoa, Error, JSON, Promise,
    document: { getElementById: () => ({}) },
    history: { replaceState() {} },
    location: {
      pathname: '/s/callback', search: '?code=abc&state=st',
      replace: (u) => navigations.push(u), assign: (u) => navigations.push(u),
    },
    fetch: async (url) => {
      if (url === '/s/static/config.json') {
        return json({ cognito_domain: 'https://auth.example', client_id: 'c', redirect_uri: 'https://share.example/s/callback' });
      }
      if (url.endsWith('/oauth2/token')) return json({ access_token: 'a', id_token: 'i' });
      if (url === `${SHARE}session`) return json({ landing });
      throw new Error(`unexpected fetch ${url}`);
    },
  });
  vm.runInContext(SOURCE, context);
  for (let i = 0; i < 50; i++) await new Promise((r) => setImmediate(r));
  return navigations;
}

test('a well-formed landing under this share navigates', async () => {
  for (const kind of ['f', 'v']) {
    const landing = `${SHARE}k/sess.ion-_1/${kind}/sub/page%20x.html`;
    assert.deepEqual(await run(landing), [landing]);
  }
});

test('hostile landings never navigate', async () => {
  const bad = [
    '//evil.example/x', 'https://evil.example/x', '/\\evil.example/x', '\\\\evil.example/x',
    `/s/${OTHER}/k/x/f/a`,                       // another share's prefix
    `/s/${OTHER}/`, '/k/x/f/a', 'k/x/f/a', '',
    `${SHARE}k/x/f/..%2f..%2fevil`, `${SHARE}k/x/f/a%2Fb`, `${SHARE}k/x/f/a%5cb`, `${SHARE}k/x/f/a\\b`,
    `${SHARE}k/x/f/../../../evil`, `${SHARE}k/x/f/%2e%2e/evil`, `${SHARE}k/x/f/./a`, `${SHARE}k/x/f/a/..`,
    `${SHARE}k/x/f/`, `${SHARE}k/x/f//evil.example`, `${SHARE}k/x/f/a?x=1`, `${SHARE}k/x/f/a#h`,
    `${SHARE}k/x/f/a\nb`, `${SHARE}k/x/g/a`, `${SHARE}k//f/a`, `${SHARE}k/${'x'.repeat(769)}/f/a`,
    `${SHARE}x/${'x'}/f/a`, ` ${SHARE}k/x/f/a`, null, 5, ['x'],
  ];
  for (const landing of bad) {
    assert.deepEqual(await run(landing), [], JSON.stringify(landing));
  }
});
