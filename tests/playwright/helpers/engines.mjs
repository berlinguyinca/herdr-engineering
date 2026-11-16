// Per-engine registration for the report tests. BROWSERS=chromium,firefox is the default (webkit opt-in);
// an engine that is not installed is skipped, never silently passed.
import { test } from 'node:test';
import fs from 'node:fs';
import * as playwright from 'playwright';

export const ENGINES = (process.env.BROWSERS || 'chromium,firefox').split(',').map(b => b.trim()).filter(Boolean);

/** Launch one engine; WebKit gets software Mesa because NVIDIA EGL hosts crash its compositor. */
export function launch(engine) {
  const options = { headless: true };
  if (engine === 'chromium') {
    options.args = ['--no-sandbox'];
    if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
  }
  if (engine === 'webkit') {
    const mesa = '/usr/share/glvnd/egl_vendor.d/50_mesa.json';
    if (fs.existsSync(mesa)) options.env = { ...process.env, __EGL_VENDOR_LIBRARY_FILENAMES: mesa, LIBGL_ALWAYS_SOFTWARE: '1' };
  }
  return playwright[engine].launch(options);
}

/** Register `fn(engine)` once per wanted engine; a missing engine skips that test. */
export function engineTest(name, fn) {
  for (const engine of ENGINES) {
    test(`${name} [${engine}]`, async (t) => {
      try { (await launch(engine)).close(); } catch (error) { return t.skip(`${engine} is not installed: ${String(error.message).split('\n')[0]}`); }
      await fn(engine);
    });
  }
}
