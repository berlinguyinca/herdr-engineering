// "Full report" button: what the button and its status line say. Pure, so it runs under `node --test`.
// Messages are plain text (the caller escapes them); the report itself is markdown the caller renders.
export const REPORT_LABELS = {idle: 'Full report', running: 'Generating report…', done: 'Regenerate full report', failed: 'Retry full report'};
export const OPEN_STATES = new Set(['queued', 'running']);

const FAILURES = {
  timeout: 'The summarizer did not finish in time.',
  harness_missing: 'The summarizer is not installed on the dashboard host.',
  no_session_record: 'No recorded conversation for this agent was found on the dashboard host.',
  malformed_report: 'The summarizer did not return a session summary.',
  summarizer_failed: 'The summarizer failed.',
  host_unreachable: "The agent's host did not pick up the request. Its session service may be stopped or not updated.",
  evidence_too_large: 'This conversation is too large to summarize.',
  audit_persistence_failed: 'The report could not be saved.',
};

export function reportFailure(code, detail) {
  const base = FAILURES[code] || 'The report could not be generated.';
  return detail && code === 'timeout' ? `${base} ${detail}.` : base;
}

// full: the server's `full_report` ({state, report?, error?, …}); local: {state:'starting'|'error', message?}.
export function reportView(full, local) {
  const f = full || {state: 'idle'};
  if (local?.state === 'starting' || OPEN_STATES.has(f.state)) {
    const waiting = f.state === 'queued' && local?.state !== 'starting';
    return {label: REPORT_LABELS.running, locked: true, report: f.report || '',
      status: waiting ? "Waiting for the agent's host to start the report." : 'Running the summary skill on this session. This can take a few minutes.'};
  }
  if (local?.state === 'error') {
    return {label: REPORT_LABELS[f.state] || REPORT_LABELS.idle, locked: false, status: local.message, error: true, report: f.report || ''};
  }
  if (f.state === 'done' && typeof f.report === 'string' && f.report) {
    const when = f.generated_at ? ` · ${f.generated_at}` : '';
    const unsaved = f.persisted === false ? ' · not saved; it disappears when the dashboard restarts' : '';
    return {label: REPORT_LABELS.done, locked: false, status: `Full report${f.source === 'terminal' ? ' · from the terminal' : f.harness ? ` · ${f.harness}` : ''}${when}${unsaved}`, report: f.report, basisSeq: Number.isFinite(f.basis_seq) ? f.basis_seq : null};
  }
  if (f.state === 'failed') {
    return {label: REPORT_LABELS.failed, locked: false, status: reportFailure(f.error, f.detail), error: true, report: ''};
  }
  return {label: REPORT_LABELS.idle, locked: false, status: '', report: ''};
}

export function reportStartError(err) {
  const text = (err && err.message) || 'unknown error';
  return {state: 'error', message: text};
}
