// "Full report" button: what the button and its status line say. Pure, so it runs under `node --test`.
// Messages are plain text (the caller escapes them); the report itself is markdown the caller renders.
export const REPORT_LABELS = {idle: 'Generate full report', running: 'Generating report…', done: 'Regenerate full report', failed: 'Retry full report'};

const FAILURES = {
  timeout: 'The summarizer did not finish in time.',
  harness_missing: 'The summarizer is not installed on the dashboard host.',
  no_session_record: 'No recorded conversation for this agent was found on the dashboard host.',
  malformed_report: 'The summarizer did not return a session summary.',
  summarizer_failed: 'The summarizer failed.',
  evidence_too_large: 'This conversation is too large to summarize.',
  audit_persistence_failed: 'The report could not be saved.',
};

export function reportFailure(code, detail) {
  const base = FAILURES[code] || 'The report could not be generated.';
  return detail && code === 'timeout' ? `${base} ${detail}.` : base;
}

// full: the server's `full_report` ({state, report?, error?, …}); local: {state:'starting'|'error', message?, command?}.
export function reportView(full, local) {
  const f = full || {state: 'idle'};
  if (local?.state === 'starting' || f.state === 'running') {
    return {label: REPORT_LABELS.running, locked: true, status: 'Running the summary skill on this session. This can take a few minutes.', report: f.report || ''};
  }
  if (local?.state === 'error') {
    return {label: REPORT_LABELS[f.state] || REPORT_LABELS.idle, locked: false, status: local.message, error: true, command: local.command || '', report: f.report || ''};
  }
  if (f.state === 'done' && typeof f.report === 'string' && f.report) {
    const when = f.generated_at ? ` · ${f.generated_at}` : '';
    const unsaved = f.persisted === false ? ' · not saved; it disappears when the dashboard restarts' : '';
    return {label: REPORT_LABELS.done, locked: false, status: `Full report${f.harness ? ` · ${f.harness}` : ''}${when}${unsaved}`, report: f.report};
  }
  if (f.state === 'failed') {
    return {label: REPORT_LABELS.failed, locked: false, status: reportFailure(f.error, f.detail), error: true, report: ''};
  }
  return {label: REPORT_LABELS.idle, locked: false, status: '', report: ''};
}

export function reportStartError(err) {
  const body = (err && err.body) || {};
  const text = (err && err.message) || 'unknown error';
  return {state: 'error', message: text, command: body.reason === 'report_host_remote' && typeof body.command === 'string' ? body.command : ''};
}
