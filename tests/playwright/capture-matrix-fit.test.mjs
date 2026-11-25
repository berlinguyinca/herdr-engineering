// The capture harness grows the viewport to the page height so a sticky 100vh sidebar spans the whole full-page screenshot.
import test from 'node:test';
import assert from 'node:assert/strict';
import { launch } from './ui-gates-lib.mjs';
import { fitViewportToPage } from './capture-matrix.mjs';

const HTML = '<style>body{margin:0;display:flex}aside{position:sticky;top:0;height:100vh;width:100px;background:#357}main{height:3000px;flex:1}</style><aside id=s></aside><main></main>';

test('fitViewportToPage makes the sticky 100vh sidebar as tall as the document', async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
    await page.setContent(HTML);
    assert.equal(await page.evaluate(() => document.getElementById('s').offsetHeight), 600);
    const h = await fitViewportToPage(page, 800, 600);
    assert.equal(h, 3000);
    assert.equal(await page.evaluate(() => document.getElementById('s').offsetHeight), 3000);
    assert.equal(await page.evaluate(() => innerWidth), 800);
  } finally { await browser.close(); }
});

test('fitViewportToPage respects the cap and never shrinks below the mode height', async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
    await page.setContent(HTML);
    assert.equal(await fitViewportToPage(page, 800, 600, 1500), 1500);
    const short = await browser.newPage({ viewport: { width: 800, height: 600 } });
    await short.setContent('<p>short</p>');
    assert.equal(await fitViewportToPage(short, 800, 600), 600);
  } finally { await browser.close(); }
});
