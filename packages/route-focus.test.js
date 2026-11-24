import test from 'node:test';
import assert from 'node:assert/strict';
import { pageTitle, APP_TITLE } from './route-focus.js';

test('the title names the page, preferring the h1 over the route label', () => {
  assert.equal(pageTitle('Hosts', 'Fabric'), `Hosts — ${APP_TITLE}`);
  assert.equal(pageTitle('  ', 'Hosts'), `Hosts — ${APP_TITLE}`);
  assert.equal(pageTitle(null, ''), APP_TITLE);
  assert.equal(pageTitle('Mission\n  abc', ''), `Mission abc — ${APP_TITLE}`);
});

