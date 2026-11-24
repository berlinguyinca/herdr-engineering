// node shoot.js <dir>: screenshot every terminal-*.html in <dir> to PNG with headless Chromium (Playwright).
const { chromium } = require('/opt/homebrew/lib/node_modules/playwright');
const fs = require('fs'), path = require('path');
(async () => {
  const dir = process.argv[2];
  const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
  const page = await browser.newPage({ deviceScaleFactor: 2 });
  for (const f of fs.readdirSync(dir).filter(f => /^terminal-.*\.html$/.test(f))) {
    await page.goto('file://' + path.resolve(dir, f));
    await page.locator('#t').screenshot({ path: path.join(dir, f.replace('.html', '.png')) });
  }
  await browser.close();
})();
