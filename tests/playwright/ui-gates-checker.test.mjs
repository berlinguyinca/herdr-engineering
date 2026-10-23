// Self-test of the in-house accessibility checker (a11y-checks.mjs): it must FLAG known-bad markup, otherwise a green
// audit of the real page proves nothing. Each case is a minimal page with exactly one defect.
// Run: node --test tests/playwright/ui-gates-checker.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { launch } from './ui-gates-lib.mjs';
import { staticAudit, focusIndicatorAudit, tabTrapAudit, overflowAudit, motionAudit } from './a11y-checks.mjs';

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser?.close(); });

const head = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>t</title><style>body{margin:0;font:16px sans-serif;color:#111;background:#fff}</style>';
async function page(html, opts = {}) {
  const p = await browser.newPage(opts);
  await p.setContent(`${head}${opts.css ? `<style>${opts.css}</style>` : ''}</head><body>${html}</body></html>`);
  return p;
}
const rules = (f) => [...new Set(f.map((x) => x.rule))];

const BAD = [
  ['an unnamed button', '<button><svg width="10" height="10"></svg></button>', 'name-role-value'],
  ['grey-on-white text below 4.5:1', '<p style="color:#8e8ea0">low contrast text here</p>', 'color-contrast'],
  ['a tiny target with a neighbour', '<button style="width:12px;height:12px;padding:0">x</button><button style="width:12px;height:12px;padding:0">y</button>', 'target-size'],
  ['a tab outside a tablist', '<button role="tab" aria-selected="false">One</button>', 'tab-needs-tablist'],
  ['an input without a label', '<input type="text">', 'label'],
  ['a clickable div that cannot be focused', '<div style="cursor:pointer" onclick="1">click me</div>', 'keyboard-operable'],
  ['a viewport that blocks zoom', '<p>x</p><script>document.head.querySelector("meta[name=viewport]").content="width=device-width, user-scalable=no"</script>', 'zoom-not-blocked'],
  ['an aria-controls pointing nowhere', '<button aria-controls="nope">go</button>', 'aria-ref'],
  ['a duplicate id', '<p id="a">1</p><p id="a">2</p>', 'duplicate-id'],
  ['an image without alt', '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">', 'image-alt'],
];
for (const [name, html, rule] of BAD) {
  test(`static audit flags ${name}`, async () => {
    const p = await page(html);
    const found = rules(await staticAudit(p));
    assert.ok(found.includes(rule), `expected "${rule}", got ${JSON.stringify(found)}`);
    await p.close();
  });
}

test('static audit passes a well-formed page', async () => {
  const p = await page('<h1>Title</h1><button style="min-width:44px;min-height:44px">Save</button><label>Name <input type="text"></label><p>Body text</p>');
  assert.deepEqual(await staticAudit(p), []);
  await p.close();
});

test('hover-only reveal is flagged and a focus equivalent clears it', async () => {
  const bad = await page('<div class="a"><button class="b">x</button></div>', { css: '.b{opacity:0}.a:hover .b{opacity:1}' });
  assert.ok(rules(await staticAudit(bad)).includes('hover-has-equivalent'));
  const ok = await page('<div class="a"><button class="b">x</button></div>', { css: '.b{opacity:0}.a:hover .b,.a:focus-within .b{opacity:1}' });
  assert.ok(!rules(await staticAudit(ok)).includes('hover-has-equivalent'));
  await bad.close(); await ok.close();
});

test('focus audit flags a removed focus indicator', async () => {
  const p = await page('<button>plain</button>', { css: 'button:focus,button:focus-visible{outline:none}' });
  assert.ok(rules(await focusIndicatorAudit(p)).includes('focus-visible'));
  const ok = await page('<button>plain</button>');
  assert.deepEqual(await focusIndicatorAudit(ok), []);
  await p.close(); await ok.close();
});

test('tab-trap audit flags a trap and passes a normal page', async () => {
  const trap = await page('<input id="a"><script>document.getElementById("a").addEventListener("keydown",e=>{if(e.key==="Tab")e.preventDefault()})</script>');
  assert.equal((await tabTrapAudit(trap, 30)).trapped, true);
  const ok = await page('<button>a</button><button>b</button><button>c</button>');
  assert.equal((await tabTrapAudit(ok, 30)).trapped, false);
  await trap.close(); await ok.close();
});

test('overflow audit flags horizontal scrolling at 320px', async () => {
  const p = await page('<div style="width:900px;height:20px;background:#ccc"></div>', { viewport: { width: 320, height: 500 } });
  const o = await overflowAudit(p);
  assert.ok(o.scrollWidth > o.clientWidth);
  const ok = await page('<p>fits</p>', { viewport: { width: 320, height: 500 } });
  const k = await overflowAudit(ok);
  assert.ok(k.scrollWidth <= k.clientWidth);
  await p.close(); await ok.close();
});

test('motion audit flags animation that ignores reduced motion', async () => {
  const p = await page('<div class="m">m</div>', { reducedMotion: 'reduce', css: '.m{animation:s 2s linear infinite}@keyframes s{to{transform:translateX(10px)}}' });
  assert.ok((await motionAudit(p)).length > 0);
  const ok = await page('<div class="m">m</div>', { reducedMotion: 'reduce', css: '.m{animation:s 2s linear infinite}@keyframes s{to{transform:translateX(10px)}}@media (prefers-reduced-motion:reduce){.m{animation:none}}' });
  assert.deepEqual(await motionAudit(ok), []);
  await p.close(); await ok.close();
});
