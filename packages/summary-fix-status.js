// "Let the agent fix this": what the button and its status line say after the server answers.
// Pure, so it runs under `node --test`. Messages are plain text (the caller escapes them); a link
// is a separate field so no markup ever rides inside a message.
export const HELPERS_LINK = {href: '#/collab', label: 'Open Helpers'};

export const FIX_LABELS = {sending: 'Sending…', sent: 'Sent to agent', queued: 'Queued for agent', helper: 'Helper started'};
export const FIX_LOCKED = new Set(['sending', 'sent', 'queued', 'helper']);

export function fixOutcome(receipt) {
  const r = receipt || {};
  if (r.status === 'helper_started' || r.delivery === 'helper') {
    return {state: 'helper', message: 'Started a helper (no tools): result appears under Helpers. Nothing it writes is applied.', link: HELPERS_LINK};
  }
  if (r.status === 'delivered') return {state: 'sent', message: 'Delivered to the agent. Watch the conversation for its reply.'};
  if (r.status === 'queued') return {state: 'queued', message: 'Queued; delivery is not confirmed yet.'};
  return {state: 'error', message: `Not delivered: ${r.error || r.status || 'unknown error'}.`};
}

export function fixError(err) {
  const body = (err && err.body) || {};
  const text = (err && err.message) || 'unknown error';
  if (body.fallback === 'helper_unavailable') return {state: 'error', message: `${text} Start the agent again, then retry.`};
  if (body.reason === 'stale_item') return {state: 'error', message: 'This item changed since it was shown. The summary was refreshed; try again.', reload: true};
  if (body.reason === 'unsupported_harness') return {state: 'error', message: text};
  if (body.fallback === 'helper_limit' || body.fallback === 'helpers_paused') {
    return {state: 'error', message: `${text} Try again later.`, link: HELPERS_LINK};
  }
  if (body.fallback) return {state: 'error', message: text};
  return {state: 'error', message: `Could not send to the agent: ${text}`};
}
