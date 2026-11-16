import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');

async function withReports(callback, env = {}) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/artifact-reports-server.py')], {
    cwd: ROOT, env: { ...process.env, ...env }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser;
  try {
    const port = await new Promise((resolve, reject) => {
      let output = '';
      const timeout = setTimeout(() => reject(new Error('Report server did not start')), 20000);
      server.stdout.on('data', data => { output += data; const match = output.match(/PORT=(\d+)/); if (match) { clearTimeout(timeout); resolve(Number(match[1])); } });
      server.stderr.on('data', data => process.stderr.write(`[report-server] ${data}`));
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Report server exited ${code}`)); });
    });
    const options = { headless: true, args: ['--no-sandbox'] };
    if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
    browser = await chromium.launch(options);
    const page = await browser.newPage();
    const requests = [];
    page.on('request', request => requests.push({ url: request.url(), method: request.method() }));
    await callback({ page, requests, base: `http://127.0.0.1:${port}` });
  } finally {
    await browser?.close();
    server.kill();
  }
}

test('report reader uses versioned metadata and fetches preview/evidence only on demand', async () => {
  await withReports(async ({ page, base, requests }) => {
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.locator('[data-report-version]').waitFor();
    await page.getByText('Finding from version 2.', { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.reportXss), undefined);
    assert.equal(await page.locator('[data-report-version]').inputValue(), 'version_2');
    assert.equal(requests.filter(r => /\/assets\//.test(r.url)).length, 0);
    assert.equal(requests.filter(r => r.url.endsWith('/preview')).length, 0);
    await page.getByRole('button', { name: 'Load lightweight preview', exact: true }).click();
    const preview = page.locator('[data-static-preview]');
    await preview.waitFor();
    // Locators pierce the open shadow root of the static copy.
    await preview.getByRole('heading', { name: 'Findings', exact: true }).waitFor();
    assert.equal(await page.locator('iframe').count(), 0);
    assert.equal(page.frames().length, 1);
    assert.equal(await preview.locator('a').count(), 0);
    assert.match(await preview.getAttribute('style'), /contain:\s*paint/);
    await preview.getByRole('heading', { name: 'Findings', exact: true }).click();
    assert.equal(page.url(), `${base}/#/artifacts/report_1`);
    await page.getByText('Scripts, frames, audio and video are not shown in this static copy', { exact: true }).waitFor();
    assert.equal(requests.filter(r => r.url.endsWith('/versions/version_2/preview/render')).length, 1);
    assert.equal(requests.filter(r => r.url.endsWith('/versions/version_2/preview')).length, 0);
    assert.equal(requests.filter(r => /\/assets\//.test(r.url)).length, 0);
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Download evidence/timing.csv', exact: true }).click();
    const file = await download;
    assert.equal(file.suggestedFilename(), 'timing.csv');
    assert.equal(requests.filter(r => r.url.endsWith('/versions/version_2/assets/observations')).length, 1);
  });
});

test('feedback remains on its immutable version and checks retain separate stages', async () => {
  await withReports(async ({ page, base, requests }) => {
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.locator('[data-report-feedback-form]').waitFor();
    await page.locator('[data-report-feedback-target]').selectOption(JSON.stringify(['finding', 'finding_latency']));
    await page.locator('[data-report-feedback-body]').fill('Please clarify this denominator.');
    await page.getByRole('button', { name: 'Add feedback to this version', exact: true }).click();
    await page.locator('[data-report-feedback-list]').getByText('Please clarify this denominator.', { exact: true }).waitFor();
    await page.locator('[data-report-version]').selectOption('version_1');
    await page.getByText('Finding from version 1.', { exact: true }).waitFor();
    assert.equal(await page.locator('[data-report-feedback-list]').getByText('Please clarify this denominator.').count(), 0);
    await page.locator('[data-report-version]').selectOption('version_2');
    await page.locator('[data-report-feedback-list]').getByText('Please clarify this denominator.', { exact: true }).waitFor();
    assert.match(await page.locator('[data-check-stage="rendered"]').innerText(), /Passed/);
    assert.match(await page.locator('[data-check-stage="evidence_checked"]').innerText(), /Not checked/);
    assert.match(await page.locator('[data-check-stage="reviewed"]').innerText(), /Not checked/);
    await page.locator('[data-report-check-stage]').selectOption('evidence_checked');
    await page.locator('[data-report-check-notes]').fill('Compared the observation rows and denominator.');
    await page.locator('[data-report-check-reference][value="evidence:timing"]').check();
    await page.getByRole('button', { name: 'Record check for this version', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[data-check-stage="evidence_checked"]')?.textContent.includes('Passed'));
    assert.match(await page.locator('[data-check-stage="reviewed"]').innerText(), /Not checked/);
    await page.reload();
    await page.waitForFunction(() => document.querySelector('[data-check-stage="evidence_checked"]')?.textContent.includes('Passed'));
    await page.locator('[data-report-check-stage]').selectOption('reviewed');
    await page.locator('[data-report-check-notes]').fill('Reviewed the finding against its cited evidence.');
    await page.locator('[data-report-check-reference][value="finding:finding_latency"]').check();
    await page.getByRole('button', { name: 'Record check for this version', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[data-check-stage="reviewed"]')?.textContent.includes('Passed'));
    await page.locator('[data-report-version]').selectOption('version_1');
    await page.getByText('Finding from version 1.', { exact: true }).waitFor();
    assert.match(await page.locator('[data-check-stage="evidence_checked"]').innerText(), /Not checked/);
    assert.match(await page.locator('[data-check-stage="reviewed"]').innerText(), /Not checked/);
    const changes = requests.filter(r => r.method === 'POST' && /\/(feedback|checks)$/.test(r.url));
    assert.equal(changes.length, 3);
    assert.ok(changes.every(r => r.url.includes('/versions/version_2/')));
  });
});

test('semantic differences label data, analysis, editorial and presentation changes', async () => {
  await withReports(async ({ page, base }) => {
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.locator('[data-report-diff-base]').waitFor();
    await page.locator('[data-report-diff-base]').selectOption('version_1');
    await page.getByRole('button', { name: 'Compare versions', exact: true }).click();
    await page.locator('[data-diff-category="editorial"]').waitFor();
    assert.match(await page.locator('[data-diff-category="editorial"]').innerText(), /Changed/);
    assert.match(await page.locator('[data-diff-category="data"]').innerText(), /No change/);
    assert.match(await page.locator('[data-diff-category="analysis"]').innerText(), /No change/);
    assert.match(await page.locator('[data-diff-category="presentation"]').innerText(), /No change/);
    assert.match(await page.locator('[data-report-diff-result]').innerText(), /version_1.*version_2/s);
  });
});

test('late version responses cannot replace the currently selected report', async () => {
  await withReports(async ({ page, base }) => {
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.getByText('Finding from version 2.', { exact: true }).waitFor();
    await page.locator('[data-report-version]').selectOption('version_1');
    await page.locator('[data-report-version]').selectOption('version_2');
    await page.getByText('Finding from version 2.', { exact: true }).waitFor();
    await page.waitForTimeout(500);
    assert.equal(await page.getByText('Finding from version 1.', { exact: true }).count(), 0);
    assert.equal(await page.locator('[data-report-version]').inputValue(), 'version_2');
    await page.locator('[data-report-version]').selectOption('version_1');
    await page.getByRole('link', { name: '← Artifacts', exact: true }).click();
    await page.locator('[data-artifact-registry]').waitFor();
    await page.waitForTimeout(500);
    assert.equal(await page.locator('[data-report-feedback-form]').count(), 0);
  }, { REPORT_DELAY: '0.3' });
});

test('figure feedback remains accessible beyond the first bounded page', async () => {
  await withReports(async ({ page, base, requests }) => {
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.locator('[data-report-feedback-list]').getByText('Figure observation 10', { exact: true }).waitFor();
    assert.equal(await page.getByText('Figure observation 25', { exact: true }).count(), 0);
    await page.getByRole('button', { name: 'Load more feedback', exact: true }).click();
    await page.locator('[data-report-feedback-list]').getByText('Figure observation 25', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Load more feedback', exact: true }).count(), 0);
    const fetch = requests.find(request => request.url.includes('/versions/version_2/feedback?'));
    assert.ok(fetch?.url.includes('offset=10'));
    assert.ok(fetch?.url.includes('limit=20'));
    await page.locator('[data-report-feedback-list]').getByRole('button', { name: 'figure: figure_latency', exact: true }).last().click();
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.itemId), 'figure_latency');
    await page.getByRole('button', { name: 'Comment on figure', exact: true }).click();
    assert.equal(await page.locator('[data-report-feedback-target]').inputValue(), JSON.stringify(['figure', 'figure_latency']));
  }, { REPORT_MANY_COMMENTS: '1' });
});

test('failed metadata and comparison requests stay visible instead of becoming empty review states', async () => {
  await withReports(async ({ page, base }) => {
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.getByRole('alert').filter({ hasText: 'Report version could not load' }).waitFor();
    assert.equal(await page.getByText('No feedback recorded for this version.', { exact: true }).count(), 0);
    assert.equal(await page.locator('[data-check-stage]').count(), 0);
    assert.equal(await page.getByRole('button', { name: 'Retry this version' }).count(), 1);
  }, { REPORT_FAIL: 'version_2' });
  await withReports(async ({ page, base }) => {
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.locator('[data-report-diff-base]').selectOption('version_1');
    await page.getByRole('button', { name: 'Compare versions', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Comparison unavailable' }).waitFor();
    assert.equal(await page.locator('[data-diff-category]').count(), 0);
  }, { DIFF_FAIL: '1' });
});

test('report controls and asset tables fit a narrow reader viewport', async () => {
  await withReports(async ({ page, base }) => {
    await page.setViewportSize({ width: 390, height: 900 });
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.getByText('Finding from version 2.', { exact: true }).waitFor();
    const sizes = await page.evaluate(() => ({ width: innerWidth, scroll: document.body.scrollWidth,
      overflow: [...document.querySelectorAll('[data-artifact-detail] *')].filter(node => node.getBoundingClientRect().right > innerWidth + 1).slice(0, 8).map(node => node.tagName + '.' + node.className) }));
    assert.ok(sizes.scroll <= sizes.width, JSON.stringify(sizes));
    if (process.env.HERDR_REPORT_VISUAL_EVIDENCE) {
      fs.mkdirSync(process.env.HERDR_REPORT_VISUAL_EVIDENCE, { recursive: true });
      await page.screenshot({ path: path.join(process.env.HERDR_REPORT_VISUAL_EVIDENCE, 'report-mobile.png') });
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.screenshot({ path: path.join(process.env.HERDR_REPORT_VISUAL_EVIDENCE, 'report-desktop.png') });
    }
  });
});

test('a legacy logical artifact opens the report reader when its version summary declares a report', async () => {
  await withReports(async ({ page, base, requests }) => {
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.getByText('Finding from version 2.', { exact: true }).waitFor();
    assert.equal(await page.locator('[data-report-version]').inputValue(), 'version_2');
    assert.equal(await page.locator('[data-report-feedback-form]').count(), 1);
    assert.equal(await page.locator('[data-report-check-form]').count(), 1);
    assert.equal(await page.locator('[data-report-diff-base]').count(), 1);
    assert.equal(await page.getByRole('button', { name: 'Load lightweight preview', exact: true }).count(), 1);
    assert.equal(requests.filter(request => request.url.endsWith('/artifacts/report_1/download')).length, 0);
  }, { ARTIFACT_REPORT_LEGACY_TYPE: '1' });
});

test('mixed report histories preserve legacy output previews and downloads on the selected version', async () => {
  await withReports(async ({ page, base, requests }) => {
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.getByText('Finding from version 2.', { exact: true }).waitFor();
    assert.equal(await page.locator('[data-report-diff-base] option[value="version_1"]').count(), 0);
    await page.locator('[data-report-version]').selectOption('version_1');
    await page.getByRole('heading', { name: 'Legacy report version', exact: true }).waitFor();
    assert.equal(await page.locator('[data-report-feedback-form]').count(), 0);
    assert.equal(requests.filter(request => /\/outputs\//.test(request.url)).length, 0);
    await page.getByRole('button', { name: 'Preview index.html', exact: true }).click();
    await page.locator('[data-static-preview]').getByRole('heading', { name: 'Legacy version_1' }).waitFor();
    assert.equal(page.frames().length, 1);
    const downloading = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Download index.html', exact: true }).click();
    const downloaded = await downloading;
    assert.match(fs.readFileSync(await downloaded.path(), 'utf8'), /Legacy version_1/);
    assert.equal(requests.filter(request => request.url.endsWith('/versions/version_1/outputs/0/render')).length, 1);
    assert.equal(requests.filter(request => request.url.endsWith('/versions/version_1/outputs/0')).length, 1);
    assert.equal(requests.filter(request => request.url.endsWith('/artifacts/report_1/download')).length, 0);
    assert.equal(requests.filter(request => request.url.endsWith('/versions/version_1/report')).length, 0);
    await page.locator('[data-report-version]').selectOption('version_2');
    await page.getByText('Finding from version 2.', { exact: true }).waitFor();
    assert.equal(await page.locator('[data-report-legacy]').count(), 0);
  }, { REPORT_MIXED_HISTORY: '1' });
});

test('legacy Markdown report history opens its pinned version directly', async () => {
  await withReports(async ({ page, base, requests }) => {
    await page.goto(`${base}/#/artifacts/report_1?version=version_1`);
    await page.getByRole('heading', { name: 'Legacy report version', exact: true }).waitFor();
    assert.equal(await page.locator('[data-report-version]').inputValue(), 'version_1');
    await page.getByRole('button', { name: 'Preview index.html', exact: true }).click();
    await page.locator('[data-static-preview]').getByRole('heading', { name: 'Legacy version_1' }).waitFor();
    assert.equal(page.frames().length, 1);
    assert.equal(requests.filter(request => request.url.endsWith('/versions/version_2/outputs/0')).length, 0);
    assert.equal(requests.filter(request => request.url.endsWith('/artifacts/report_1/download')).length, 0);
  }, { REPORT_ALL_LEGACY: '1' });
});

test('an HTML artifact shows a static copy: its script never runs on the dashboard and no frame exists', async () => {
  await withReports(async ({ page, base, requests }) => {
    await page.goto(`${base}/#/artifacts/html_1`);
    const preview = page.locator('[data-static-preview]');
    await preview.getByRole('heading', { name: 'Plain page heading' }).waitFor();
    assert.equal(await page.evaluate(() => window.__x), undefined);
    assert.equal(await page.locator('iframe').count(), 0);
    assert.equal(page.frames().length, 1);
    assert.equal(await preview.locator('a').count(), 0);
    await preview.getByText('Body text').click();
    assert.equal(page.url(), `${base}/#/artifacts/html_1`);
    assert.equal(requests.filter(r => r.url.endsWith('/artifacts/html_1/render')).length, 1);
    assert.equal(requests.filter(r => r.url.endsWith('/artifacts/html_1/download')).length, 0);
  });
});

test('a PDF artifact is drawn on a canvas with visible ink and no frame', async () => {
  await withReports(async ({ page, base }) => {
    await page.goto(`${base}/#/artifacts/pdf_1`);
    await page.locator('.hd-pdf-page[data-drawn="true"] canvas').waitFor({ timeout: 20000 });
    const inked = await page.evaluate(() => {
      const canvas = document.querySelector('.hd-pdf-page canvas');
      const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      for (let i = 0; i < data.length; i += 4) if (data[i + 3] > 0 && (data[i] < 200 || data[i + 1] < 200 || data[i + 2] < 200)) return true;
      return false;
    });
    assert.equal(inked, true);
    assert.equal(await page.locator('iframe, embed, object').count(), 0);
    assert.equal(page.frames().length, 1);
  });
});

test('the live link opens the raw file in an opaque-origin sandbox, and the dashboard origin is untouched', async () => {
  await withReports(async ({ page, base }) => {
    await page.goto(`${base}/#/artifacts/html_1`);
    const link = page.locator('[data-open-live]');
    await link.waitFor();
    assert.equal(await link.getAttribute('rel'), 'noopener noreferrer');
    assert.equal(await link.getAttribute('target'), '_blank');
    const popupPromise = page.context().waitForEvent('page');
    await link.click();
    const popup = await popupPromise;
    await popup.waitForLoadState('domcontentloaded');
    assert.match(popup.url(), /\/api\/v1\/artifacts\/html_1\/live$/);
    await popup.getByRole('heading', { name: 'Plain page heading' }).waitFor();
    const facts = await popup.evaluate(() => {
      let domainThrows = false;
      try { void document.domain; void window.parent.document.cookie; } catch { domainThrows = true; }
      let storageThrows = false;
      try { void localStorage.length; } catch { storageThrows = true; }
      return { origin: window.origin, ran: window.__x, storageThrows, domainThrows };
    });
    assert.equal(facts.origin, 'null');
    assert.equal(facts.ran, 1);
    assert.equal(facts.storageThrows, true);
    assert.equal(await page.evaluate(() => window.__x), undefined);
  });
});

test('a hostile :host fixed-position payload cannot cover or capture clicks on the dashboard', async () => {
  await withReports(async ({ page, base }) => {
    await page.setViewportSize({ width: 1000, height: 800 });
    await page.goto(`${base}/#/artifacts/host_1`);
    const preview = page.locator('[data-static-preview]');
    await preview.getByText('Hostile host page').waitFor();
    const link = page.locator('[data-open-live]');
    await link.scrollIntoViewIfNeeded();
    const hit = await link.evaluate(node => {
      const box = node.getBoundingClientRect();
      const top = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
      return top === node || node.contains(top);
    });
    assert.equal(hit, true, 'the dashboard control must stay hit-testable');
    const heading = page.locator('h2').first();
    const boxOf = await heading.boundingBox();
    const topAtHeading = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.tagName, { x: boxOf.x + 4, y: boxOf.y + 4 });
    assert.equal(topAtHeading, 'H2');
    const wrapper = await preview.boundingBox();
    assert.ok(wrapper.width <= 1000 && wrapper.height <= 640 + 2, JSON.stringify(wrapper));
  });
});

test('a very tall PDF page stays under the canvas pixel cap', async () => {
  await withReports(async ({ page, base }) => {
    await page.goto(`${base}/#/artifacts/pdf_tall`);
    await page.locator('.hd-pdf-page[data-drawn="true"] canvas').waitFor({ timeout: 20000 });
    const pixels = await page.evaluate(() => { const c = document.querySelector('.hd-pdf-page canvas'); return c.width * c.height; });
    assert.ok(pixels <= 16e6 + 1e5, String(pixels));
  });
});
