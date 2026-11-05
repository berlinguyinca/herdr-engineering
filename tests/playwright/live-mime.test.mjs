// Real-browser regression for the /live route: a stored media type like `application/pdf;a=1,text/html` must not
// make a browser run the bytes as an unsandboxed HTML page on the dashboard origin.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');

test('hostile stored media types never execute script on the dashboard origin', async () => {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/live-mime-server.py')],
    { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT }, stdio: ['pipe', 'pipe', 'inherit'] });
  let browser, buffer = '';
  try {
    const info = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('server did not start')), 30000);
      server.stdout.on('data', d => { buffer += d; try { const v = JSON.parse(buffer.split('\n')[0]); clearTimeout(timer); resolve(v); } catch { /* wait */ } });
      server.once('exit', code => { clearTimeout(timer); reject(new Error(`server exited ${code}`)); });
    });
    const options = { headless: true, args: ['--no-sandbox'] };
    if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
    browser = await chromium.launch(options);
    const context = await browser.newContext({ extraHTTPHeaders: { 'X-API-Key': 'k' } });
    for (const [index, id] of info.ids.entries()) {
      const page = await context.newPage();
      const dialogs = [];
      page.on('dialog', d => { dialogs.push(d.message()); d.dismiss(); });
      const response = await page.goto(`${info.base}/api/v1/artifacts/${id}/live`).catch(() => null);
      await page.waitForTimeout(500);
      const title = await page.title().catch(() => '');
      assert.ok(!title.startsWith('XSS:'), `script ran on the dashboard origin for ${info.mimes[index]} (title ${title})`);
      if (response) {
        const headers = response.headers();
        assert.ok(!/[,"\\]/.test(headers['content-type'] || ''), headers['content-type']);
        assert.ok((headers['content-security-policy'] || '').startsWith('sandbox') || headers['content-type'] === 'application/pdf', JSON.stringify(headers));
      }
      await page.close();
    }
  } finally {
    await browser?.close();
    server.kill();
  }
});
