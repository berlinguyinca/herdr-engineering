import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fixOutcome, fixError, FIX_LABELS, FIX_LOCKED, HELPERS_LINK} from '../summary-fix-status.js';

test('a started helper says so, links to Helpers and locks the button', () => {
  const out = fixOutcome({ok: true, status: 'helper_started', delivery: 'helper', helper_id: 'collab_h1'});
  assert.equal(out.state, 'helper');
  assert.match(out.message, /^Started a helper \(no tools\): result appears under Helpers/);
  assert.deepEqual(out.link, HELPERS_LINK);
  assert.equal(HELPERS_LINK.href, '#/collab');
  assert.ok(FIX_LOCKED.has('helper') && FIX_LABELS.helper === 'Helper started');
  assert.ok(!/[<>]/.test(out.message), 'markup never rides inside the message');
});

test('wake receipts keep their existing wording', () => {
  assert.equal(fixOutcome({status: 'delivered'}).state, 'sent');
  assert.equal(fixOutcome({status: 'queued'}).state, 'queued');
  assert.equal(fixOutcome({status: 'failed', error: 'transport_error'}).message, 'Not delivered: transport_error.');
  assert.equal(fixOutcome({status: 'delivered'}).link, undefined);
});

test('refusals: unconfigured, stale, limits and others', () => {
  const e = (message, body) => Object.assign(new Error(message), {body});
  assert.match(fixError(e('The agent is not running.', {fallback: 'helper_unavailable'})).message, /Start the agent again, then retry\.$/);
  assert.equal(fixError(e('x', {reason: 'stale_item'})).reload, true);
  const limit = fixError(e('No helper was started: live helper limit reached for this agent.', {fallback: 'helper_limit'}));
  assert.match(limit.message, /Try again later\.$/);
  assert.deepEqual(limit.link, HELPERS_LINK);
  assert.equal(fixError(e('No helper was started: forbidden.', {fallback: 'helper_refused'})).message, 'No helper was started: forbidden.');
  assert.equal(fixError(e('boom', {})).message, 'Could not send to the agent: boom');
  assert.equal(fixError(undefined).state, 'error');
});
