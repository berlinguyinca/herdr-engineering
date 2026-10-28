import test from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';

test('recorded history pages independently and preserves an older page through live updates', async () => {
  await withPage(async (page, base) => {
    await page.goto(base + '/__blank');
    await page.evaluate(async () => {
      const {mountEventHistory} = await import('/event-history.js');
      document.body.innerHTML = '<div id="events"></div><input aria-label="Other input">';
      window.newEvents = false;
      const event = index => ({event_id: `e${index}`, sequence: index,
        entity_type: 'mission', entity_id: 'pi:mission', event_type: 'MissionCreated',
        payload: {title: index === 6 ? '<img src=x onerror=alert(1)>' : `Event ${index}`}});
      window.requests = [];
      const client = {request: async (url, options) => {
        window.requests.push({url, signal: Boolean(options.signal)});
        const query = new URL(url, location.origin).searchParams;
        if (query.get('before') === 'older') return {events: [1, 2, 3].map(event), has_more: false};
        if (query.get('limit') === '1') return {events: [event(window.newEvents ? 7 : 6)]};
        return {events: (window.newEvents ? [5, 6, 7] : [4, 5, 6]).map(event), has_more: true, next_cursor: 'older'};
      }};
      window.historyView = mountEventHistory(document.querySelector('#events'), client, 'mission', 'pi:mission');
      await window.historyView.ready;
    });
    assert.equal(await page.locator('[data-event-history-rows] .hd-card').count(), 3);
    assert.equal(await page.locator('[data-event-history-rows] img').count(), 0);
    await page.getByRole('button', {name: 'Older recorded events'}).click();
    await page.waitForFunction(() => document.querySelector('[data-event-history-rows]')?.textContent.includes('Event 1'));
    await page.evaluate(async () => {window.newEvents = true; await window.historyView.refresh();});
    assert.match(await page.locator('[data-event-history-rows]').innerText(), /Event 1/);
    await page.getByRole('button', {name: 'Return to latest recorded events'}).click();
    await page.waitForFunction(() => document.querySelector('[data-event-history-rows]')?.textContent.includes('Event 7'));
    assert.ok(await page.evaluate(() => window.requests.every(r => r.signal && new URL(r.url, location.origin).searchParams.get('entity_id') === 'pi:mission')));
  });
});

test('slow history exposes progress and cancels on disposal', async () => {
  await withPage(async (page, base) => {
    await page.goto(base + '/__blank');
    await page.evaluate(async () => {
      const {mountEventHistory} = await import('/event-history.js');
      document.body.innerHTML = '<div id="events"></div>';
      window.cancelled = false;
      window.historyView = mountEventHistory(document.querySelector('#events'), {
        request: (_url, {signal}) => new Promise((_resolve, reject) => {
          signal.addEventListener('abort', () => {window.cancelled = true; reject(signal.reason);}, {once: true});
        }),
      }, 'session', 'private:session');
    });
    await page.getByRole('progressbar', {name: 'Loading recorded events'}).waitFor();
    await page.evaluate(() => window.historyView.dispose());
    await page.waitForFunction(() => window.cancelled);
  });
});

test('a failed refresh retains recorded events and an explicit retry recovers', async () => {
  await withPage(async (page, base) => {
    await page.goto(base + '/__blank');
    await page.evaluate(async () => {
      const {mountEventHistory} = await import('/event-history.js');
      document.body.innerHTML = '<div id="events"></div>';
      window.historyFailure = false;
      window.historyView = mountEventHistory(document.querySelector('#events'), {
        request: async () => {
          if (window.historyFailure) throw Object.assign(new Error('backend offline'), {status: 503, requestId: 'history-request'});
          return {events: [{event_id: 'kept', sequence: 4, event_type: 'MissionCreated',
            entity_type: 'mission', entity_id: 'm', payload: {title: 'Retained event'}}]};
        },
      }, 'mission', 'm');
      await window.historyView.ready;
      window.historyFailure = true;
      await window.historyView.refresh();
    });
    assert.match(await page.locator('[data-event-history-rows]').innerText(), /Retained event/);
    assert.match(await page.locator('[data-event-history-status]').innerText(), /history-request/);
    await page.evaluate(() => {window.historyFailure = false;});
    await page.getByRole('button', {name: 'Retry recorded events'}).click();
    await page.waitForFunction(() => document.querySelector('[data-history-action="retry"]')?.hidden);
    assert.match(await page.locator('[data-event-history-rows]').innerText(), /Retained event/);
  });
});

// The fake API uses stable before boundaries and a moving latest window, just
// like stream_page. Keep assertions on visible rows, requests, and focus.
async function mountPagedHistory(page, base, options = {}) {
  await page.goto(base + '/__blank');
  await page.evaluate(async ({failOlderOnce = false, delayOlder = false}) => {
    const {mountEventHistory} = await import('/event-history.js');
    document.body.innerHTML = '<div id="events"></div>';
    window.historyEnd = 150;
    window.historyRequests = [];
    window.releaseOlderHistory = null;
    const event = index => ({event_id: `e${index}`, sequence: index,
      entity_type: 'session', entity_id: 'history-session', event_type: 'SessionStarted',
      payload: {agent_role: `Event ${index}`}});
    const client = {request: async url => {
      const query = new URL(url, location.origin).searchParams;
      const before = query.get('before');
      const limit = Number(query.get('limit'));
      window.historyRequests.push({before, limit});
      if (before !== null && failOlderOnce) {
        failOlderOnce = false;
        throw Object.assign(new Error('Older page unavailable'), {status: 503});
      }
      const end = before === null ? window.historyEnd : Number(before);
      const start = Math.max(0, end - limit);
      const response = {events: Array.from({length: end - start}, (_, i) => event(start + i)),
        has_more: start > 0, next_cursor: start ? String(start) : null};
      if (before !== null && delayOlder) {
        delayOlder = false;
        return new Promise(resolve => {window.releaseOlderHistory = () => resolve(response);});
      }
      return response;
    }};
    window.historyView = mountEventHistory(document.querySelector('#events'), client,
      'session', 'history-session');
    await window.historyView.ready;
  }, options);
}

const historyKeys = (start, end) => Array.from({length: end - start}, (_, i) => `|e${start + i}`);
const visibleHistoryKeys = page => page.locator('[data-event-history-rows] .hd-card')
  .evaluateAll(rows => rows.map(row => row.dataset.key));
async function waitForHistoryKeys(page, expected) {
  await page.waitForFunction(keys => {
    const actual = [...document.querySelectorAll('[data-event-history-rows] .hd-card')]
      .map(row => row.dataset.key);
    return JSON.stringify(actual) === JSON.stringify(keys);
  }, expected, {timeout: 5000});
}

test('Newer restores the visited snapshot after appends and stays pinned until Latest', async () => {
  await withPage(async (page, base) => {
    await mountPagedHistory(page, base);
    const original = await visibleHistoryKeys(page);
    assert.deepEqual(original, historyKeys(100, 150));
    await page.getByRole('button', {name: 'Older recorded events'}).click();
    await waitForHistoryKeys(page, historyKeys(50, 100));
    await page.evaluate(() => {window.historyEnd = 160;});
    const requestsBeforeNewer = await page.evaluate(() => window.historyRequests.length);
    await page.getByRole('button', {name: 'Newer recorded events'}).click();
    await waitForHistoryKeys(page, original);
    assert.equal(await page.evaluate(() => window.historyRequests.length), requestsBeforeNewer,
      'returning to a visited page must not request the moving latest window');
    // At the bottom, scroll/selection protection alone cannot hide a missing pin.
    await page.evaluate(async () => {
      const rows = document.querySelector('[data-event-history-rows]');
      rows.scrollTop = rows.scrollHeight;
      await window.historyView.refresh();
    });
    assert.deepEqual(await visibleHistoryKeys(page), original);
    assert.deepEqual(await page.evaluate(() => window.historyRequests.at(-1)), {before: null, limit: 1});
    await page.getByRole('button', {name: 'Return to latest recorded events'}).click();
    await waitForHistoryKeys(page, historyKeys(110, 160));
    await page.evaluate(async () => {
      window.historyEnd = 170;
      const rows = document.querySelector('[data-event-history-rows]');
      rows.scrollTop = rows.scrollHeight;
      await window.historyView.refresh();
    });
    assert.deepEqual(await visibleHistoryKeys(page), historyKeys(120, 170));
    await page.evaluate(() => window.historyView.dispose());
  });
});

test('Retry repeats a failed Older request and Newer returns to the original page', async () => {
  await withPage(async (page, base) => {
    await mountPagedHistory(page, base, {failOlderOnce: true});
    const original = await visibleHistoryKeys(page);
    await page.getByRole('button', {name: 'Older recorded events'}).click();
    await page.getByRole('button', {name: 'Retry recorded events'}).waitFor();
    assert.deepEqual(await visibleHistoryKeys(page), original);
    const failedRequest = await page.evaluate(() => window.historyRequests.at(-1));
    assert.deepEqual(failedRequest, {before: '100', limit: 50});
    await page.getByRole('button', {name: 'Retry recorded events'}).click();
    await waitForHistoryKeys(page, historyKeys(50, 100));
    assert.deepEqual(await page.evaluate(() => window.historyRequests.at(-1)), failedRequest);
    await page.getByRole('button', {name: 'Newer recorded events'}).click();
    await waitForHistoryKeys(page, original);
    assert.equal(await page.getByRole('button', {name: 'Newer recorded events'}).isDisabled(), true);
    await page.evaluate(() => window.historyView.dispose());
  });
});

test('a delayed page response keeps focus in rows when the user moves there while loading', async () => {
  await withPage(async (page, base) => {
    await mountPagedHistory(page, base, {delayOlder: true});
    await page.getByRole('button', {name: 'Older recorded events'}).click();
    await page.waitForFunction(() => typeof window.releaseOlderHistory === 'function');
    const rows = page.getByRole('list', {name: 'Recorded event entries'});
    await rows.focus();
    assert.equal(await rows.evaluate(element => document.activeElement === element), true);
    await page.evaluate(() => window.releaseOlderHistory());
    await waitForHistoryKeys(page, historyKeys(50, 100));
    // The click handler restores focus after its awaited load finishes. A frame
    // ensures that continuation ran before asserting the user's retained focus.
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    assert.equal(await rows.evaluate(element => document.activeElement === element), true);
    await page.evaluate(() => window.historyView.dispose());
  });
});
