// Self-test of the in-house accessibility checker (a11y-checks.mjs). A green audit of the real page proves nothing unless the
// checker is known to FLAG each defect it claims to find and to leave the corrected markup alone, so every rule has a known-bad
// and a known-good fixture here. Each fixture is a minimal page with exactly one point at issue.
// Run: node --test tests/playwright/ui-gates-checker.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { launch } from './ui-gates-lib.mjs';
import { staticAudit, focusIndicatorAudit, tabTrapAudit, overflowAudit, overflowFailure, motionAudit, liveAnnouncementAudit } from './a11y-checks.mjs';

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser?.close(); });

const IMG = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
const head = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>t</title><style>body{margin:0;font:16px sans-serif;color:#111;background:#fff}</style>';
async function page(html, opts = {}) {
  const p = await browser.newPage(opts);
  await p.setContent(`${head}${opts.css ? `<style>${opts.css}</style>` : ''}</head><body><main>${html}</main></body></html>`);
  return p;
}
const rules = (f) => [...new Set(f.map((x) => x.rule))];

// [rule, [bad fixtures], [good fixtures]]; a fixture is [name, html, css?]. A bad one must raise `rule`, a good one must not.
const STATIC = [
  ['name-role-value', [
    ['an icon-only button', '<button><svg width="30" height="30"></svg></button>'],
    ['a div with role=button and no text', '<div role="button" tabindex="0" style="width:30px;height:30px"></div>'],
    ['an empty aria-label', '<button aria-label="" style="width:30px;height:30px"></button>'],
    ['aria-labelledby pointing at an empty element', '<span id="e"></span><button aria-labelledby="e" style="width:30px;height:30px"></button>'],
    ['an image with alt="" turned into a button', `<img src="${IMG}" alt="" role="button" tabindex="0" width="30" height="30">`],
    ['a select named only by its option text', '<select><option>Fork from this commit</option><option>another</option></select>'],
    ['a select with text NEXT to it that is not a label', '<span>Snooze for</span><select><option>1 hour</option></select>'],
    ['a textarea named only by its content', '<textarea>my draft comment</textarea>'],
    ['a select with an empty aria-label and options', '<select aria-label=""><option>1 hour</option></select>']],
  [['aria-label', '<button aria-label="Close" style="width:30px;height:30px"></button>'], ['text', '<button>Save</button>'],
    ['aria-labelledby with text', '<span id="n">Search</span><button aria-labelledby="n" style="width:30px;height:30px"></button>'],
    ['a select with aria-label', '<select aria-label="Snooze"><option>1 hour</option></select>'],
    ['a select inside its label', '<label>Snooze for <select><option>1 hour</option></select></label>'],
    ['a select with a for= label', '<label for="s">Base commit</label><select id="s"><option>abc</option></select>'],
    ['a select with a title', '<select title="Snooze"><option>1 hour</option></select>'],
    ['a textarea with aria-labelledby', '<span id="t">Comment</span><textarea aria-labelledby="t"></textarea>']]],
  ['color-contrast', [
    ['grey text on white', '<p style="color:#8e8ea0">low contrast text here</p>'],
    ['light text on a light gradient (measured on pixels)', '<p style="background-image:linear-gradient(#fff,#eee);color:#ddd">faint text</p>'],
    ['light text on an image background', `<p style="background-image:url(${IMG});color:#eee">faint</p>`],
    ['text faded by an ANCESTOR opacity', '<div style="opacity:.3"><p style="color:#000">faint by parent opacity</p></div>'],
    ['text painted transparent through -webkit-text-fill-color', '<p style="color:#000;-webkit-text-fill-color:transparent">invisible text</p>'],
    ['a pale -webkit-text-fill-color over a dark colour', '<p style="color:#000;-webkit-text-fill-color:#eee">pale fill</p>'],
    ['text faded by its own opacity', '<p style="opacity:.3;color:#000">faint</p>'],
    ['a translucent text colour', '<p style="color:rgba(0,0,0,.2)">alpha text</p>'],
    ['SVG text with a pale fill', '<svg width="200" height="40"><text x="5" y="20" fill="#eee">svg faint</text></svg>'],
    ['SVG text over a pale shape', '<svg width="200" height="40"><rect width="200" height="40" fill="#fff"/><text x="5" y="20" fill="#ddd">svg faint</text></svg>'],
    ['generated content (::before)', '<p class="pe"></p>', 'p.pe::before{content:"faint pseudo";color:#eee}'],
    ['a placeholder', '<input aria-label="x" placeholder="faint placeholder">', '::placeholder{color:#eee;opacity:1}'],
    ['white on yellow', '<p style="background:#ffff00;color:#fff">white on yellow</p>']],
  [['dark text on white', '<p style="color:#111">fine</p>'],
    ['dark text over a light gradient (pixels)', '<p style="background-image:linear-gradient(#fff,#eee);color:#222">readable</p>'],
    ['SVG text with a dark fill', '<svg width="200" height="40"><text x="5" y="20" fill="#111">svg fine</text></svg>'],
    ['a readable placeholder', '<input aria-label="x" placeholder="readable">', '::placeholder{color:#555;opacity:1}'],
    ['pale text in a disabled button (exempt)', '<button disabled style="color:#ccc;background:#fff">disabled</button>']]],
  ['focus-order', [
    ['a row-reverse flex row of buttons', '<div style="display:flex;flex-direction:row-reverse"><button>one</button><button>two</button></div>'],
    ['a button moved with order', '<div style="display:flex"><button style="order:2">one</button><button>two</button></div>'],
    ['a grid item moved with order', '<div style="display:grid"><button style="order:-1">one</button><button>two</button></div>']],
  [['a plain flex row', '<div style="display:flex"><button>one</button><button>two</button></div>'],
    ['a reversed row with nothing focusable in it', '<div style="display:flex;flex-direction:row-reverse"><span>one</span><span>two</span></div>'],
    ['order on something that is not focusable', '<div style="display:flex"><span style="order:2">one</span><button>two</button></div>']]],
  ['aria-hidden-focusable', [
    ['a focusable button inside aria-hidden', '<div aria-hidden="true"><button>Hidden but focusable</button></div>'],
    ['aria-hidden on the focusable element itself', '<button aria-hidden="true">x</button>']],
  [['aria-hidden with the control removed from the tab order', '<div aria-hidden="true"><button tabindex="-1">Hidden</button></div>'], ['decorative only', '<span aria-hidden="true">*</span> <button>OK</button>']]],
  ['nested-interactive', [
    ['a button inside a role=button', '<div role="button" tabindex="0" aria-label="a"><button>b</button></div>'],
    ['a link inside a button', '<button>x <a href="#">y</a></button>']],
  [['two sibling controls', '<button>a</button> <button>b</button>']]],
  ['img-wraps-interactive', [
    ['an SVG role=img around focusable shapes', '<svg role="img" aria-label="x" width="100" height="40"><g role="button" tabindex="0" aria-label="a"><rect width="50" height="40"/></g></svg>']],
  [['the same SVG as role=group', '<svg role="group" aria-label="x" width="100" height="40"><g role="button" tabindex="0" aria-label="a"><rect width="50" height="40"/></g></svg>'],
    ['an image with no interactive content', '<svg role="img" aria-label="x" width="100" height="40"><rect width="50" height="40"/></svg>']]],
  ['invalid-role', [['a misspelt role', '<div role="buttn" tabindex="0">x</div>']], [['a real role', '<div role="button" tabindex="0">x</div>']]],
  ['positive-tabindex', [['tabindex=5', '<button tabindex="5">a</button><button>b</button>']], [['tabindex 0 and -1', '<button tabindex="0">a</button><button tabindex="-1">b</button>']]],
  ['live-region', [
    ['role=status that is also assertive', '<div role="status" aria-live="assertive">x</div>'],
    ['role=alert that is also polite', '<div role="alert" aria-live="polite">x</div>'],
    ['an invalid aria-live value', '<div aria-live="loud">x</div>'],
    ['a toast that is not a live region', '<div class="toast">Saved</div>']],
  [['a polite status', '<div role="status">Saved</div>'], ['an alert', '<div role="alert">Failed</div>'], ['a toast that is a status', '<div class="toast" role="status">Saved</div>']]],
  ['transition-all', [['transition: all', '<button>a</button>', 'button{transition:all .3s}']], [['named properties', '<button>a</button>', 'button{transition:opacity .3s,background-color .3s}']]],
  ['target-size', [['two tiny targets close together', '<button aria-label="x" style="width:12px;height:12px;padding:0">x</button><button aria-label="y" style="width:12px;height:12px;padding:0">y</button>']],
    [['a 44px button', '<button style="min-width:44px;min-height:44px">Save</button>']]],
  ['tab-needs-tablist', [['a tab outside a tablist', '<button role="tab" aria-selected="false">One</button>']], [['a tab in a tablist', '<div role="tablist"><button role="tab" aria-selected="true">One</button></div>']]],
  ['label', [['an input without a label', '<input type="text">'], ['a textarea with only a placeholder', '<textarea placeholder="p"></textarea>']], [['a labelled input', '<label>Name <input type="text"></label>']]],
  ['keyboard-operable', [['a clickable div that cannot be focused', '<div style="cursor:pointer">click me</div>'],
    ['clickable options of a listbox NO combobox controls', '<ul id="lb" role="listbox" aria-label="x"><li role="option" style="cursor:pointer">one</li></ul>']], [['a real button', '<button style="cursor:pointer;min-height:30px">click me</button>'],
    ['options of a listbox that a combobox controls (arrow keys, aria-activedescendant)', '<input role="combobox" aria-label="x" aria-controls="lb" aria-expanded="true"><ul id="lb" role="listbox"><li role="option" style="cursor:pointer">one</li></ul>']]],
  ['aria-ref', [['an aria-controls pointing nowhere', '<button aria-controls="nope">go</button>']], [['an aria-controls that resolves', '<button aria-controls="yes">go</button><div id="yes"></div>']]],
  ['duplicate-id', [['a duplicate id', '<p id="a">1</p><p id="a">2</p>']], [['unique ids', '<p id="a">1</p><p id="b">2</p>']]],
  ['image-alt', [[`an image without alt`, `<img src="${IMG}">`]], [['an image with alt', `<img src="${IMG}" alt="a dot">`]]],
  ['heading-order', [['h1 followed by h3', '<h1>a</h1><h3>c</h3>']], [['h1, h2, h3', '<h1>a</h1><h2>b</h2><h3>c</h3>']]],
  ['hover-has-equivalent', [['a hover-only reveal', '<div class="a"><button class="b">x</button></div>', '.b{opacity:0}.a:hover .b{opacity:1}']],
    [['a reveal that focus also triggers', '<div class="a"><button class="b">x</button></div>', '.b{opacity:0}.a:hover .b,.a:focus-within .b{opacity:1}']]],
];
for (const [rule, bad, good] of STATIC) {
  for (const [name, html, css] of bad) {
    test(`static audit flags "${rule}": ${name}`, async () => {
      const p = await page(html, { css });
      const found = rules(await staticAudit(p));
      assert.ok(found.includes(rule), `expected "${rule}", got ${JSON.stringify(found)}`);
      await p.close();
    });
  }
  for (const [name, html, css] of good) {
    test(`static audit leaves "${rule}" alone: ${name}`, async () => {
      const p = await page(html, { css });
      const found = rules(await staticAudit(p));
      assert.ok(!found.includes(rule), `did not expect "${rule}", got ${JSON.stringify(found)}`);
      await p.close();
    });
  }
}

test('static audit flags a viewport that blocks zoom, a missing <main> and a missing lang', async () => {
  const p = await browser.newPage();
  await p.setContent('<!doctype html><html><head><meta name="viewport" content="width=device-width, user-scalable=no"><title>t</title></head><body><p>x</p></body></html>');
  const found = rules(await staticAudit(p));
  for (const r of ['zoom-not-blocked', 'landmark-main', 'html-lang']) assert.ok(found.includes(r), `expected ${r}, got ${found}`);
  await p.close();
});

test('static audit passes a well-formed page and reports how much it looked at', async () => {
  const p = await page('<h1>Title</h1><button style="min-width:44px;min-height:44px">Save</button><label>Name <input type="text"></label><p>Body text</p>');
  const f = await staticAudit(p);
  assert.deepEqual([...f], []);
  assert.ok(f.stats.controls >= 2 && f.stats.textNodes >= 3, `stats ${JSON.stringify(f.stats)}`);
  await p.close();
});

test('pixel contrast is only used for image and gradient backgrounds, and says so', async () => {
  const p = await page('<p style="background-image:linear-gradient(#fff,#eee);color:#ddd">faint text</p>');
  const f = await staticAudit(p);
  assert.ok(f.stats.pixelChecked >= 1);
  assert.match(f.find((x) => x.rule === 'color-contrast').detail, /rendered pixels/);
  await p.close();
});

// ---- focus indicator
const FOCUS_BAD = [
  ['an outline removed', '<button>plain</button>', 'button:focus,button:focus-visible{outline:none}', 'focus-visible'],
  ['a white ring on a white page', '<button style="border:0">x</button>', 'button:focus{outline:2px solid #fff}', 'focus-indicator-contrast'],
  ['a nearly transparent ring', '<button>faint ring</button>', 'button:focus{outline:2px solid rgba(0,0,0,.02)}', 'focus-indicator-contrast'],
  ['a one-shade background change', '<button style="background:#fff;border:0">x</button>', 'button:focus{outline:none;background:#fefefe!important}', 'focus-indicator-contrast'],
  ['a focusable control that is transparent', '<button style="opacity:0">invisible</button>', '', 'focus-visible'],
  ['a focusable control parked off screen', '<button style="position:absolute;left:-9999px">off</button>', '', 'focus-visible'],
  ['a control hidden under a sticky header', '<div style="position:fixed;top:0;left:0;right:0;height:60px;background:#ccc;z-index:5"></div><div style="height:30px"></div><button>under header</button>', '', 'focus-not-obscured'],
];
for (const [name, html, css, rule] of FOCUS_BAD) {
  test(`focus audit flags "${rule}": ${name}`, async () => {
    const p = await page(html, { css });
    const f = await focusIndicatorAudit(p);
    assert.ok(rules(f).includes(rule), `expected ${rule}, got ${JSON.stringify(rules(f))}`);
    await p.close();
  });
}
test('focus audit leaves a visible ring alone and reports how many controls it focused', async () => {
  const p = await page('<button>plain</button> <a href="#x">link</a>', { css: 'button:focus,a:focus{outline:3px solid #00338d;outline-offset:2px}' });
  const f = await focusIndicatorAudit(p);
  assert.deepEqual([...f], []);
  assert.equal(f.stats.focused, 2);
  await p.close();
});
test('focus audit accepts the browser default ring', async () => {
  const p = await page('<button>plain</button>');
  assert.deepEqual([...(await focusIndicatorAudit(p))], []);
  await p.close();
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

test('overflow audit is not fooled by content cut off behind overflow:hidden: the document does not scroll, but the content is lost', async () => {
  const p = await page('<div style="overflow-x:hidden"><div style="width:900px;height:20px;background:#ccc"></div></div>', { viewport: { width: 320, height: 500 } });
  const o = await overflowAudit(p);
  assert.ok(o.scrollWidth <= o.clientWidth, 'a clipping wrapper means the document itself does not scroll');
  assert.match(overflowFailure(o), /content is cut off/);
  await p.close();
});

test('overflow audit allows an ellipsis, a scroll container and overflow:hidden that cuts nothing', async () => {
  for (const html of [
    '<p style="width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">a long line that does not fit in its box at all</p>',
    '<div style="width:100px;overflow-x:auto"><div style="width:400px;height:10px;background:#ccc"></div></div>',
    '<div style="overflow:hidden;border-radius:8px"><p>fits</p></div>',
  ]) {
    const p = await page(html, { viewport: { width: 320, height: 500 } });
    assert.equal(overflowFailure(await overflowAudit(p)), '', html);
    await p.close();
  }
});

const MOTION_BAD = [
  ['an endless animation', '<div class="m">m</div>', '.m{animation:s 2s linear infinite}@keyframes s{to{transform:translateX(10px)}}'],
  ['a long transition that is not "all"', '<button class="m">a</button>', 'button{transition:opacity .5s}'],
  ['transition: all', '<button class="m">a</button>', 'button{transition:all 1s}'],
  ['smooth scrolling', '<p>a</p>', 'html{scroll-behavior:smooth}'],
];
for (const [name, html, css] of MOTION_BAD) {
  test(`motion audit flags ${name} that ignores reduced motion`, async () => {
    const p = await page(html, { reducedMotion: 'reduce', css });
    assert.ok((await motionAudit(p)).length > 0);
    await p.close();
  });
}
test('motion audit passes when reduced motion switches the motion off', async () => {
  const ok = await page('<div class="m">m</div><button>b</button>', { reducedMotion: 'reduce', css: '.m{animation:s 2s linear infinite}@keyframes s{to{transform:translateX(10px)}}button{transition:all 1s}@media (prefers-reduced-motion:reduce){.m{animation:none}button{transition:none}}' });
  assert.deepEqual(await motionAudit(ok), []);
  await ok.close();
});

// ---- dynamic: a change that nobody is told about
const LIVE = (attrs) => `<button id="go" onclick="document.getElementById('out').textContent='Saved ' + Date.now()">go</button><div id="out" ${attrs}>idle</div>`;
test('live announcement audit flags a status message that is not a live region', async () => {
  const p = await page(LIVE(''));
  // the page's own inline handler is blocked nowhere here (set via setContent), so click and watch
  const f = await liveAnnouncementAudit(p, () => p.click('#go'), '#out');
  assert.ok(rules(f).includes('live-announcement'), `got ${JSON.stringify(f)}`);
  assert.ok(f.stats.changed >= 1);
  await p.close();
});
test('live announcement audit accepts role=status, role=alert and aria-live=polite', async () => {
  for (const attrs of ['role="status"', 'role="alert"', 'aria-live="polite"']) {
    const p = await page(LIVE(attrs));
    const f = await liveAnnouncementAudit(p, () => p.click('#go'), '#out');
    assert.deepEqual([...f], [], attrs);
    assert.ok(f.stats.changed >= 1, `${attrs}: the change was seen`);
    await p.close();
  }
});
test('live announcement audit rejects aria-live=off', async () => {
  const p = await page(LIVE('aria-live="off"'));
  assert.ok(rules(await liveAnnouncementAudit(p, () => p.click('#go'), '#out')).includes('live-announcement'));
  await p.close();
});
