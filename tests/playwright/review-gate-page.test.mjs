// The Review gate page polls with the browser's own timers. Its default timers were methods of a plain object, so the browser
// refused the call ("Illegal invocation") and the page showed an error. Real timers, no fake clock: a fake clock hides this.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';

test('review gate page loads with real timers and shows no error', async () => withPage(async (page, base) => {
  await page.goto(`${base}/#/review-gate`);
  await page.waitForSelector('#view [data-review-gate], #view herdr-error', {timeout: 15000}).catch(() => {});
  await page.waitForTimeout(1500);
  const failure = await page.evaluate(() => document.querySelector('#view herdr-error')?.getAttribute('message') || document.querySelector('#view herdr-error')?.textContent || '');
  assert.equal(failure, '', 'the page did not fail');
  assert.ok((await page.locator('#view').innerText()).trim().length > 0, 'the page rendered something');
}));
