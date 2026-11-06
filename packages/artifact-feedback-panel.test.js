import test from 'node:test';
import assert from 'node:assert/strict';
import { renderAnchor, renderAnnotationRow, mountFeedbackPanel, EMPTY_TEXT } from './artifact-feedback-panel.js';
import { renderEvents } from './artifact-share-panel.js';

const IMG = '<img src=x onerror=alert(1)>';
const assertInert = html => {
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html.replace(/"[^"]*"/g, '""').replace(/>[^<]*</g, '><'), /\son[a-z]+=/i);
};
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const base = { annotation_id: 'a1', author_email: 'v@lab.org', version_number: 2, status: 'open', body: 'hi', anchor: { kind: 'document', file: 'index.html' } };

test('anchors render per kind, escaped, always with the file', () => {
  assert.match(renderAnchor({ kind: 'text_quote', file: 'a.html', prefix: 'p', exact: 'E', suffix: 's' }), /…p<mark>E<\/mark>s… in <code>a\.html<\/code>/);
  const pdf = renderAnchor({ kind: 'pdf_page', file: 'a.pdf', page: 3, exact: 'q', region: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 } });
  assert.match(pdf, /Page 3/); assert.match(pdf, /<mark>q<\/mark>/); assert.match(pdf, /10%, 20% \(30% × 40%\)/);
  assert.match(renderAnchor({ kind: 'pdf_page', file: 'a.pdf', page: 1 }), /^Page 1 in <code>a\.pdf/);
  assert.match(renderAnchor({ kind: 'image_region', file: 'i.png', x: 0.5, y: 0.25, w: 0.1, h: 0.2 }), /Region 50%, 25% \(10% × 20%\) in <code>i\.png/);
  assert.match(renderAnchor({ kind: 'document', file: 'd' }), /Whole file in <code>d<\/code>/);
});

test('hostile email, body, quote and file name are escaped', () => {
  const html = renderAnnotationRow({ ...base, author_email: IMG, body: IMG, anchor: { kind: 'text_quote', file: IMG, prefix: IMG, exact: IMG, suffix: IMG } }, { artifactId: 'art1' });
  assertInert(html);
  assert.match(html, /&lt;img/);
});

test('open rows have Dismiss; addressed rows link to the version and have none', () => {
  assert.match(renderAnnotationRow(base, { artifactId: 'art1' }), /data-feedback-action="dismiss" data-annotation-id="a1"/);
  const html = renderAnnotationRow({ ...base, status: 'addressed', addressed_in_version_id: 'v9', addressed_in_version_number: 3 }, { artifactId: 'art1' });
  assert.doesNotMatch(html, /data-feedback-action="dismiss"/);
  assert.match(html, /href="#\/artifacts\/art1\?version=v9">addressed in v3</);
});

test('owner badge, edited marker, busy and unbroken body wrap', () => {
  const html = renderAnnotationRow({ ...base, author_role: 'owner', edited_at: '2026-01-01T00:00:00Z', body: 'x'.repeat(4000) }, { artifactId: 'a', busy: true });
  assert.match(html, /owner/); assert.match(html, /\(edited\)/); assert.match(html, /white-space:pre-wrap;overflow-wrap:anywhere/);
  assert.match(html, /data-feedback-action="dismiss"[^>]* disabled/);
  assert.doesNotMatch(renderAnnotationRow(base, {}), /owner|edited/);
});

test('renderEvents labels the annotation kinds', () => {
  const html = renderEvents([{ kind: 'annotated' }, { kind: 'annotation_withdrawn' }]);
  assert.match(html, /<strong>annotated<\/strong>/);
  assert.match(html, /annotation deleted by its author/);
});

function fake() {
  const listeners = {};
  const c = { html: '', isConnected: true, listeners,
    get innerHTML() { return c.html; }, set innerHTML(v) { c.html = String(v); },
    replaceChildren() { c.html = ''; },
    addEventListener(t, f) { (listeners[t] ||= new Set()).add(f); }, removeEventListener(t, f) { listeners[t]?.delete(f); },
    fire(type, el) { for (const f of listeners[type] || []) f({ target: { closest: () => el } }); } };
  return c;
}
const ok = rows => ({ artifact_id: 'art1', annotations: rows, next: null });

test('empty state, default Open filter and a filter change reload', async () => {
  const calls = [];
  const client = { listArtifactAnnotations: async (id, o) => { calls.push(o.status); return ok([]); } };
  const c = fake();
  const h = mountFeedbackPanel(c, client, 'art1', {});
  await tick();
  assert.deepEqual(calls, ['open']);
  assert.ok(c.html.includes(EMPTY_TEXT.replace(/&/g, '&amp;')));
  c.fire('change', { value: 'all' }); await tick();
  assert.deepEqual(calls, ['open', 'all']);
  h.dispose();
});

test('Load more appends with the cursor', async () => {
  const seen = [];
  const client = { listArtifactAnnotations: async (id, o) => { seen.push(o.before); return o.before ? { annotations: [{ ...base, annotation_id: 'a2', body: 'second' }], next: null } : { annotations: [base], next: 'cur' }; } };
  const c = fake(); const h = mountFeedbackPanel(c, client, 'art1', {});
  await tick();
  c.fire('click', { disabled: false, dataset: { feedbackAction: 'more' } }); await tick();
  assert.deepEqual(seen, [undefined, 'cur']);
  assert.match(c.html, /second/); assert.match(c.html, /hi/); assert.doesNotMatch(c.html, /Load more/);
  h.dispose();
});

for (const [status, text] of [[503, /unavailable/], [404, /could not be found/]]) {
  test(`a ${status} shows a friendly notice with Retry and no raw error text`, async () => {
    let fail = true;
    const client = { listArtifactAnnotations: async () => { if (fail) throw Object.assign(new Error('annotation store unavailable <b>raw</b>'), { status }); return ok([base]); } };
    const c = fake(); const h = mountFeedbackPanel(c, client, 'art1', {});
    await tick();
    assert.match(c.html, /<herdr-error/); assert.match(c.html, text); assert.match(c.html, /data-feedback-action="retry"/);
    assert.doesNotMatch(c.html, /<b>|annotation store|>raw/);
    fail = false; c.fire('click', { disabled: false, dataset: { feedbackAction: 'retry' } }); await tick();
    assert.match(c.html, /hi/);
    h.dispose();
  });
}

test('Dismiss asks confirm, disables in flight, then refreshes; declined confirm does nothing', async () => {
  let dismissed = 0, release;
  const gate = new Promise(r => { release = r; });
  const client = { listArtifactAnnotations: async () => ok(dismissed ? [] : [base]), dismissArtifactAnnotation: async () => { await gate; dismissed++; return {}; } };
  const c = fake(); const h = mountFeedbackPanel(c, client, 'art1', {});
  await tick();
  const click = () => c.fire('click', { disabled: false, dataset: { feedbackAction: 'dismiss', annotationId: 'a1' } });
  const realConfirm = globalThis.confirm;
  try {
    globalThis.confirm = () => false; click(); await tick();
    assert.equal(dismissed, 0); assert.doesNotMatch(c.html, /disabled/);
    globalThis.confirm = () => true; click(); await tick();
    assert.match(c.html, /data-feedback-action="dismiss"[^>]* disabled/);
    click(); release(); await tick(); await tick();
    assert.equal(dismissed, 1); assert.ok(c.html.includes('No annotations yet'));
  } finally { globalThis.confirm = realConfirm; }
  h.dispose();
});

test('a failed dismiss shows a fixed notice, not the server text', async () => {
  const client = { listArtifactAnnotations: async () => ok([base]), dismissArtifactAnnotation: async () => { throw Object.assign(new Error('secret'), { status: 503 }); } };
  const c = fake(); const h = mountFeedbackPanel(c, client, 'art1', {});
  await tick();
  const realConfirm = globalThis.confirm; globalThis.confirm = () => true;
  try { c.fire('click', { disabled: false, dataset: { feedbackAction: 'dismiss', annotationId: 'a1' } }); await tick(); } finally { globalThis.confirm = realConfirm; }
  assert.match(c.html, /could not be dismissed/); assert.doesNotMatch(c.html, /secret/);
  h.dispose();
});

test('dispose removes listeners and aborts', async () => {
  let sig;
  const client = { listArtifactAnnotations: async (id, o) => { sig = o.signal; return ok([]); } };
  const c = fake(); const h = mountFeedbackPanel(c, client, 'art1', {});
  await tick(); h.dispose();
  assert.equal(sig.aborted, true);
  assert.equal([...Object.values(c.listeners)].reduce((n, s) => n + s.size, 0), 0);
});

test('every viewer-controlled field is escaped, including attribute breakouts', () => {
  const evil = '"><img src=x onerror=alert(1)>';
  const hostile = {
    annotation_id: evil, author_email: evil, author_role: evil, version_number: evil, status: evil, body: evil,
    created_at: evil, edited_at: evil, addressed_in_version_id: evil, addressed_in_version_number: evil,
    anchor: { kind: 'pdf_page', file: evil, page: evil, exact: evil, region: { x: evil, y: 0, w: 0, h: 0 } },
  };
  for (const row of [{ ...hostile, status: 'open' }, hostile, { ...hostile, anchor: { kind: 'text_quote', file: evil, prefix: evil, exact: evil, suffix: evil } },
    { ...hostile, anchor: { kind: 'image_region', file: evil, x: evil, y: evil, w: evil, h: evil } }, { ...hostile, anchor: { kind: evil, file: evil } }]) {
    assertInert(renderAnnotationRow(row, { artifactId: evil }));
  }
  const open = renderAnnotationRow({ ...hostile, status: 'open' }, { artifactId: evil });
  assert.match(open, /data-annotation-id="&quot;&gt;&lt;img/);
  assert.doesNotMatch(open, /"><img/);
  assert.doesNotMatch(renderAnnotationRow(hostile, { artifactId: evil }), /"><img|<img/);
  assertInert(renderEvents([{ kind: evil, at: evil, actor: evil, detail: evil }]));
});

test('a failed dismiss (409, 404, 503) reloads so the row has no live Dismiss', async () => {
  for (const status of [409, 404, 503]) {
    let gone = false;
    const client = {
      listArtifactAnnotations: async () => ok(gone ? [{ ...base, status: 'dismissed' }] : [base]),
      dismissArtifactAnnotation: async () => { gone = true; throw Object.assign(new Error('x'), { status }); },
    };
    const c = fake(); const h = mountFeedbackPanel(c, client, 'art1', {});
    await tick();
    const real = globalThis.confirm; globalThis.confirm = () => true;
    try { c.fire('click', { disabled: false, dataset: { feedbackAction: 'dismiss', annotationId: 'a1' } }); await tick(); await tick(); } finally { globalThis.confirm = real; }
    assert.doesNotMatch(c.html, /data-feedback-action="dismiss"/, String(status));
    assert.match(c.html, status === 409 ? /no longer open/ : /could not be dismissed/);
    h.dispose();
  }
});

test('without confirm() nothing is dismissed', async () => {
  let calls = 0;
  const client = { listArtifactAnnotations: async () => ok([base]), dismissArtifactAnnotation: async () => { calls++; return {}; } };
  const c = fake(); const h = mountFeedbackPanel(c, client, 'art1', {});
  await tick();
  const real = globalThis.confirm; delete globalThis.confirm;
  try { c.fire('click', { disabled: false, dataset: { feedbackAction: 'dismiss', annotationId: 'a1' } }); await tick(); } finally { if (real) globalThis.confirm = real; }
  assert.equal(calls, 0);
  h.dispose();
});
