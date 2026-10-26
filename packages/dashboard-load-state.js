// Shared dashboard request state: preserve useful causes across independent panels.

export function classifyDashboardError(error, elapsedMs = 0) {
  const status = Number(error?.status || error?.response?.status || 0);
  const message = String(error?.message || '').toLowerCase();
  const timeout = Boolean(error?.dashboardTimeout || error?.name === 'TimeoutError' ||
    error?.name === 'AbortError' && message.includes('timeout'));
  let kind = 'unknown';
  if (timeout) kind = 'timeout';
  else if (status === 401 || status === 403) kind = 'authentication';
  else if (status === 429) kind = 'rate_limited';
  else if (status >= 500) kind = 'backend';
  else if (status >= 400) kind = 'request';
  else if (error?.name === 'AbortError') kind = 'cancelled';
  else if (message.includes('invalid response')) kind = 'invalid_response';
  else if (message.includes('failed to fetch') || message.includes('network')) kind = 'network';
  return {kind, status, elapsedMs: Math.max(0, Math.round(elapsedMs)), message: error?.message || ''};
}

export function dashboardErrorMessage(label, detail) {
  const elapsed = detail.elapsedMs ? ` after ${Math.max(1, Math.round(detail.elapsedMs / 1000))}s` : '';
  const cause = {
    timeout: 'The request exceeded the dashboard deadline.',
    authentication: 'The dashboard credentials were rejected.',
    rate_limited: 'The control plane rate limit was reached.',
    backend: 'The control plane or one of its backends returned an error.',
    network: 'The control plane could not be reached.',
    invalid_response: 'The control plane returned an invalid response.',
    request: 'The control plane rejected the request.',
    cancelled: 'The request was cancelled.',
    unknown: 'The cause was not identified.',
  }[detail.kind] || 'The cause was not identified.';
  return `${label} failed${elapsed}. ${cause}`;
}
