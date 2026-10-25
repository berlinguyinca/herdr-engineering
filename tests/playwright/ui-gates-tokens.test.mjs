// T16 UI gates, autospec-design `design.tokens.*` rules for the collaboration styles. Pure file checks (no browser).
//   * z-index and font-family must be on the declared scale / come from tokens: zero violations, enforced.
//   * raw colour literals and off-grid spacing were already widespread when this gate was added, so they are a RATCHET:
//     the count may go down, never up (budgets in ui-gates.yaml, `tokens:`). Paying the debt down is follow-up work.
// Run: node --test tests/playwright/ui-gates-tokens.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { CFG, REPO } from './ui-gates-lib.mjs';

const FILES = ['packages/collab.css', 'packages/conversation.css'];
const css = (f) => readFileSync(path.join(REPO, f), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
const declarations = (src) => [...src.matchAll(/([a-z-]+)\s*:\s*([^;{}]+)[;}]/gi)].map((m) => ({ prop: m[1].toLowerCase(), value: m[2].trim() }));

test('design.tokens.zindex-on-scale: z-index values sit on the declared scale', () => {
  const scale = new Set([0, 10, 20, 30, 40, 50, 100, 1000, 10000]);       // autospec scale {0,10,20,30,40,50,100,1000}; 10000 = the existing fullscreen composer
  const bad = FILES.flatMap((f) => declarations(css(f)).filter((d) => d.prop === 'z-index' && !scale.has(Number(d.value))).map((d) => `${f}: z-index ${d.value}`));
  assert.deepEqual(bad, []);
});

test('design.tokens.font-from-tokens: font-family references a token, not a literal stack', () => {
  const bad = FILES.flatMap((f) => declarations(css(f)).filter((d) => ['font-family', 'font'].includes(d.prop) && /[a-z]/i.test(d.value) && !/var\(--font-(sans|mono)/.test(d.value) && !/^(inherit|initial|unset)\b/.test(d.value) && d.prop === 'font-family').map((d) => `${f}: ${d.prop}: ${d.value}`));
  assert.deepEqual(bad, []);
});

test('design.tokens.no-raw-color: raw hex colours in rules (outside token definitions and var() fallbacks) never grow', () => {
  let n = 0;
  for (const f of FILES) {
    for (const d of declarations(css(f))) {
      if (d.prop.startsWith('--')) continue;                                 // a token definition is where a literal belongs
      const outside = d.value.replace(/var\([^)]*\)/g, '');                   // var(--x, #fallback) is a token reference
      n += (outside.match(/#[0-9a-f]{3,8}\b/gi) || []).length;
    }
  }
  assert.ok(n <= CFG.tokens.max_raw_hex, `raw hex colours in rules: ${n} (ratchet ${CFG.tokens.max_raw_hex}); use a token, or lower the ratchet in ui-gates.yaml when you remove some`);
});

test('design.tokens.spacing-on-scale: off-grid margin/padding/gap values never grow', () => {
  let n = 0;
  for (const f of FILES) {
    for (const d of declarations(css(f))) {
      if (!/^(margin|padding|gap|row-gap|column-gap)(-|$)/.test(d.prop) || d.prop.startsWith('--')) continue;
      for (const m of d.value.matchAll(/(-?\d+(?:\.\d+)?)px/g)) { const v = Math.abs(Number(m[1])); if (v > 2 && v % 4 !== 0) n += 1; }
    }
  }
  assert.ok(n <= CFG.tokens.max_off_grid_spacing, `off-grid spacing values: ${n} (ratchet ${CFG.tokens.max_off_grid_spacing})`);
});

test('design.tokens.status-contrast: the page\'s status text colours reach 4.5:1 on its surfaces in light and dark (WCAG 1.4.3)', () => {
  const src = css('packages/collab.css');
  const lum = (h) => {
    const c = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255).map((x) => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4));
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  const ratio = (a, b) => { const [lo, hi] = [lum(a), lum(b)].sort((x, y) => x - y); return (hi + 0.05) / (lo + 0.05); };
  const tokens = (block) => Object.fromEntries([...block.matchAll(/(--[a-z0-9-]+)\s*:\s*(#[0-9a-f]{6})\b/gi)].map((m) => [m[1], m[2].toLowerCase()]));
  const light = tokens(src.match(/\.cx-page, \.cx-embed \{([^}]*)\}/)[1]);
  const darkBlock = src.match(/@media \(prefers-color-scheme: dark\) \{\s*\.cx-page \{([^}]*)\}/);
  assert.ok(darkBlock, 'collab.css has a dark .cx-page token block');
  const dark = { ...light, ...tokens(darkBlock[1]) };
  const bad = [];
  for (const [name, t, surfaces] of [['light', light, ['#ffffff']], ['dark', dark, [dark['--background'], dark['--surface']]]]) {
    for (const tok of ['--status-blocked', '--status-attention']) {
      for (const bg of surfaces) if (t[tok] && ratio(t[tok], bg) < 4.5) bad.push(`${name}: ${tok} ${t[tok]} on ${bg} is ${ratio(t[tok], bg).toFixed(2)}:1`);
    }
  }
  assert.deepEqual(bad, []);
});
