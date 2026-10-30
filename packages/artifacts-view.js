import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';
import { morph } from './patch.js';
import { pollWhileVisible } from './refresh-bus.js';
import { mountSharePanel } from './artifact-share-panel.js';
import { mountHistory } from './artifact-history.js';

const humanSize = value => {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 ** 2).toFixed(1)} MB`;
};

export function mountArtifacts(container, client, { freshness = () => null } = {}) {
  let disposed = false, timer, controller, generation = 0, loaded = false;
  container.innerHTML = '<div class="hd-card" data-artifact-registry><div class="hd-card__title">Artifact registry</div><div class="hd-inline"><input class="hd-input" data-artifact-query placeholder="Search artifacts" aria-label="Search artifacts"><select class="hd-input" data-artifact-type aria-label="Filter artifact type"><option value="">All types</option><option value="other">Other</option><option value="web">Web</option><option value="report">Report</option><option value="document">Document</option></select><select class="hd-input" data-artifact-status aria-label="Filter artifact status"><option value="">All statuses</option><option value="available">Available</option><option value="draft">Draft</option></select><select class="hd-input" data-artifact-context-kind aria-label="Linked to"><option value="">Any work</option><option value="project">Project</option><option value="issue">Issue</option><option value="task">Task</option><option value="room">Room</option></select><input class="hd-input" data-artifact-context-ref placeholder="Project, issue, task or room id" aria-label="Linked work id"></div><p data-artifact-state role="status"></p><div data-artifact-results></div><button type="button" data-artifact-retry hidden>Retry</button></div>';
  const results = container.querySelector('[data-artifact-results]');
  const state = container.querySelector('[data-artifact-state]');
  const retry = container.querySelector('[data-artifact-retry]');
  const render = async ({ replace = false } = {}) => {
    if (disposed || (controller && !replace)) return;
    controller?.abort();
    const request = new AbortController(); controller = request;
    const current = ++generation;
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(15000)]);
    if (!loaded) results.innerHTML = skeleton('artifacts');
    state.textContent = loaded ? 'Refreshing artifacts…' : '';
    retry.hidden = true;
    try {
      const contextKind = container.querySelector('[data-artifact-context-kind]').value;
      const contextRef = container.querySelector('[data-artifact-context-ref]').value.trim();
      const q = container.querySelector('[data-artifact-query]').value;
      // A named piece of work asks the registry for what is linked to it; type and status do not apply there.
      const { artifacts } = contextKind && contextRef
        ? await client.artifactsForContext(contextKind, contextRef, { q, limit: 100, signal })
        : await client.listArtifacts({
          q,
          type: container.querySelector('[data-artifact-type]').value,
          status: container.querySelector('[data-artifact-status]').value, limit: 100, signal,
        });
      if (disposed || current !== generation) return;
      if (!Array.isArray(artifacts)) throw new Error('Invalid artifact registry response');
      morph(results, artifacts.length ? `<div class="hd-table-wrap"><table class="hd-table"><thead><tr><th>Name</th><th>Type</th><th>Size</th><th>Status</th><th>Created</th></tr></thead><tbody>${artifacts.map(artifact => `<tr><td><a href="#/artifacts/${encodeURIComponent(artifact.artifact_id)}">${esc(artifact.display_name || artifact.original_filename || artifact.artifact_id)}</a></td><td>${esc(artifact.artifact_type || 'other')}</td><td>${artifact.size_bytes == null ? '—' : humanSize(artifact.size_bytes)}</td><td>${esc(artifact.status || '—')}</td><td>${esc(artifact.created_at || 'unknown')}</td></tr>`).join('')}</tbody></table></div>` : '<herdr-empty message="No artifacts match the current filters"></herdr-empty>');
      loaded = true;
      state.textContent = artifacts.length === 100 ? 'Showing the first 100 artifacts. Refine the search to narrow results.' : '';
      freshness()?.touch(true);
    } catch (error) {
      if (disposed || current !== generation || request.signal.aborted) return;
      if (!loaded) results.innerHTML = '';
      state.textContent = `Artifacts could not load: ${error.message}`;
      retry.hidden = false;
      freshness()?.touch(false, error.message);
    } finally { if (controller === request) controller = null; }
  };
  const schedule = () => { clearTimeout(timer); timer = setTimeout(() => render({ replace: true }), 200); };
  container.querySelectorAll('[data-artifact-query],[data-artifact-type],[data-artifact-status],[data-artifact-context-kind],[data-artifact-context-ref]').forEach(control => control.addEventListener('input', schedule));
  retry.addEventListener('click', render);
  const stop = pollWhileVisible(render, 30000);
  void render();
  return { refresh: render, dispose() { disposed = true; clearTimeout(timer); stop(); controller?.abort(); } };
}

const STAGES = { rendered: 'Rendered', evidence_checked: 'Evidence checked', reviewed: 'Reviewed' };
const STATUS = { not_checked: 'Not checked', passed: 'Passed', failed: 'Failed', stale: 'Stale', unavailable: 'Unavailable' };
const CATEGORY_HELP = {
  data: 'Evidence and result content.',
  analysis: 'Recorded methods, configuration, and analysis source.',
  editorial: 'Questions, findings, explanations, limitations, and next steps.',
  presentation: 'Figures, display assets, and presentation details.',
};
const failure = error => esc(error?.message || 'The request could not be completed.');
const list = values => (values || []).map(value => `<li>${esc(value)}</li>`).join('');
const jump = (kind, id, label) => `<button type="button" class="hd-btn" data-report-jump="${esc(JSON.stringify([kind, id]))}">${esc(label || id)}</button>`;
const commentButton = (kind, id) => `<button type="button" class="hd-btn" data-report-comment="${esc(JSON.stringify([kind, id]))}">Comment on ${esc(kind)}</button>`;
const json = value => esc(JSON.stringify(value, null, 2));

function descriptorHtml(descriptor) {
  const evidence = new Map((descriptor.evidence || []).map(item => [item.id, item]));
  const figures = new Map((descriptor.figures || []).map(item => [item.id, item]));
  const questions = (descriptor.questions || []).map(item => `<li data-report-item data-kind="question" data-item-id="${esc(item.id)}" tabindex="-1"><strong>${esc(item.id)}</strong>: ${esc(item.text)}</li>`).join('');
  const findings = (descriptor.findings || []).map(item => `<article class="hd-card" data-report-item data-kind="finding" data-item-id="${esc(item.id)}" tabindex="-1"><h3>${esc(item.title)}</h3><p>${esc(item.statement)}</p><p>Questions: ${(item.question_ids || []).map(id => jump('question', id, id)).join(' ') || 'None referenced'}</p><p>Evidence: ${(item.evidence_ids || []).map(id => jump('evidence', id, evidence.get(id)?.title || id)).join(' ') || 'None referenced'}</p><p>Figures: ${(item.figure_ids || []).map(id => jump('figure', id, figures.get(id)?.title || id)).join(' ') || 'None referenced'}</p>${item.limitations?.length ? `<h4>Limits of this finding</h4><ul>${list(item.limitations)}</ul>` : ''}${item.next_steps?.length ? `<h4>Next investigations</h4><ul>${list(item.next_steps)}</ul>` : ''}${commentButton('finding', item.id)}</article>`).join('');
  const observations = (descriptor.evidence || []).map(item => `<article class="hd-card" data-report-item data-kind="evidence" data-item-id="${esc(item.id)}" tabindex="-1"><h3>${esc(item.title)}</h3><p>${esc(item.description)}</p><p>${(item.asset_ids || []).map(id => jump('asset', id, `Evidence asset: ${id}`)).join(' ')}</p></article>`).join('');
  const figureRows = (descriptor.figures || []).map(item => `<article class="hd-card" data-report-item data-kind="figure" data-item-id="${esc(item.id)}" tabindex="-1"><h3>${esc(item.title)}</h3><p>${esc(item.caption)}</p>${item.reading_guide ? `<h4>How to read this figure</h4><p>${esc(item.reading_guide)}</p>` : ''}${jump('asset', item.asset_id, 'View figure asset details')} ${commentButton('figure', item.id)}</article>`).join('');
  return `<section class="hd-card"><h2>${esc(descriptor.title || 'Report')}</h2><p>${esc(descriptor.summary || '')}</p><h3>Questions</h3><ul>${questions || '<li>No questions recorded.</li>'}</ul></section><section><h2>Findings</h2>${findings || '<p>No findings recorded.</p>'}</section><section><h2>Supporting evidence</h2>${observations || '<p>No evidence recorded.</p>'}</section><section><h2>Figures</h2>${figureRows || '<p>No figures recorded.</p>'}</section><section class="hd-card"><h2>Limitations and next steps</h2><ul>${list(descriptor.limitations)}</ul><ol>${list(descriptor.next_steps)}</ol><details><summary>Recorded analysis configuration</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${json(descriptor.analysis || {})}</pre></details></section>`;
}

function checkHtml(checks) {
  return `<section class="hd-card"><h2>Independent checks</h2><p>Rendering success does not establish that evidence was checked or that a reviewer approved this version.</p><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">${Object.entries(STAGES).map(([stage, label]) => {
    const check = checks?.[stage];
    const references = (check?.evidence_refs || []).map(reference => {
      const separator = reference.indexOf(':');
      return separator > 0 ? jump(reference.slice(0, separator), reference.slice(separator + 1), reference) : esc(reference);
    }).join(' ');
    return `<article data-check-stage="${stage}"><h3>${label}</h3><strong>${esc(STATUS[check?.status] || check?.status || 'Unavailable')}</strong>${check?.actor_id || check?.actor ? `<p>By ${esc(check.actor_id || check.actor)}</p>` : ''}${check?.created_at ? `<p>${esc(check.created_at)}</p>` : ''}${check?.notes ? `<p>${esc(check.notes)}</p>` : ''}${check?.reason ? `<p>${esc(check.reason)}</p>` : ''}${references ? `<p>Supporting items: ${references}</p>` : ''}</article>`;
  }).join('')}</div></section>`;
}

function feedbackRows(rows) {
  return rows.map(item => `<article class="hd-card"><p><strong>${item.target_id ? jump(item.target_kind, item.target_id, `${item.target_kind}: ${item.target_id}`) : 'Whole report'}</strong> · ${esc(item.version_id)}</p><p style="white-space:pre-wrap">${esc(item.body)}</p><p class="hd-note">${esc(item.actor_id || item.author_id || item.actor || 'Unknown author')} · ${esc(item.created_at || '')}</p></article>`).join('');
}

function feedbackHtml(report) {
  const descriptor = report.report_descriptor;
  const targets = `<option value="${esc(JSON.stringify(['report', null]))}">Whole report</option>` + ['finding', 'figure'].map(kind => `<optgroup label="${kind === 'finding' ? 'Findings' : 'Figures'}">${(descriptor[kind === 'finding' ? 'findings' : 'figures'] || []).map(item => `<option value="${esc(JSON.stringify([kind, item.id]))}">${esc(item.title)} (${esc(item.id)})</option>`).join('')}</optgroup>`).join('');
  const comments = feedbackRows(report.feedback || []);
  const references = ['evidence', 'finding', 'figure'].flatMap(kind => (descriptor[{ evidence: 'evidence', finding: 'findings', figure: 'figures' }[kind]] || []).map(item => `<label style="display:block"><input type="checkbox" data-report-check-reference value="${esc(`${kind}:${item.id}`)}"> ${esc(kind)}: ${esc(item.title || item.id)}</label>`)).join('');
  return `<section class="hd-card"><h2>Feedback for this version</h2><p>Feedback is pinned to <strong>${esc(report.version_id)}</strong>; changing versions does not move comments.</p><div data-report-feedback-list>${comments || '<p>No feedback recorded for this version.</p>'}</div>${report.feedback_has_more ? '<p>More comments remain attached to this version.</p><button type="button" class="hd-btn" data-report-feedback-more>Load more feedback</button><p role="status" data-report-feedback-more-status></p>' : ''}<form data-report-feedback-form><label>Finding or figure<select class="hd-input" data-report-feedback-target>${targets}</select></label><label style="display:block">Feedback<textarea class="hd-input" rows="4" maxlength="10000" required data-report-feedback-body style="width:100%"></textarea></label><button class="hd-btn" type="submit">Add feedback to this version</button><p role="status" data-report-feedback-status></p></form></section><section class="hd-card"><h2>Record a check</h2><p>Record a check you performed, with its evidence. Passing review requires the rendering and evidence checks to pass first.</p><form data-report-check-form><div class="hd-inline"><label>Stage<select class="hd-input" data-report-check-stage>${Object.entries(STAGES).map(([id, label]) => `<option value="${id}">${label}</option>`).join('')}</select></label><label>Outcome<select class="hd-input" data-report-check-status><option value="passed">Passed</option><option value="failed">Failed</option></select></label></div><label style="display:block">What was checked<textarea class="hd-input" rows="3" maxlength="10000" required data-report-check-notes style="width:100%"></textarea></label><fieldset><legend>Evidence supporting this check</legend>${references || '<p>No referenceable report items are available.</p>'}</fieldset><button class="hd-btn" type="submit">Record check for this version</button><p role="status" data-report-check-result></p></form></section>`;
}

function assetHtml(assets) {
  return `<section class="hd-card"><h2>Evidence and source downloads</h2><p>Assets remain separate from this metadata view. They load only when you request a download.</p>${assets.length ? `<div class="hd-table-wrap" style="overflow-x:auto;max-width:100%"><table class="hd-table"><thead><tr><th>Asset</th><th>Role</th><th>Format</th><th>Size</th><th>Integrity</th><th>Download</th></tr></thead><tbody>${assets.map(asset => `<tr data-report-item data-kind="asset" data-item-id="${esc(asset.id)}" tabindex="-1"><td>${esc(asset.name)}</td><td>${esc(asset.role)}</td><td>${esc(asset.media_type)}</td><td>${humanSize(asset.size_bytes)}</td><td><details><summary>SHA-256</summary><code style="overflow-wrap:anywhere">${esc(asset.sha256)}</code></details></td><td><button type="button" class="hd-btn" data-report-download="${esc(asset.id)}" aria-label="Download ${esc(asset.name)}">Download</button><span role="status" data-download-status></span></td></tr>`).join('')}</tbody></table></div>` : '<p>No assets attached.</p>'}</section>`;
}

export function renderReportDiff(diff, base, selected) {
  const bounded = diff.truncated ? `<p role="note" data-report-diff-truncated>Only bounded change details are shown. Total changes: ${esc(diff.change_count ?? 'unknown')}; detail limit: ${esc(diff.detail_limit ?? 'unknown')}. Counts include omitted details. Use the report version selector to inspect both complete descriptors.</p>` : '';
  return `<p>Changes from <strong>${esc(base)}</strong> to <strong>${esc(selected)}</strong>.</p>${bounded}${['data', 'analysis', 'editorial', 'presentation'].map(category => {
    const change = diff.categories?.[category];
    if (!change) return `<article data-diff-category="${category}"><h3>${esc(category)}</h3><p>Comparison category unavailable.</p></article>`;
    const kinds = ['added', 'removed', 'modified'];
    const totals = kinds.map(kind => `${esc(change.counts?.[kind] ?? Object.keys(change[kind] || []).length)} ${kind}`).join(' · ');
    const omitted = kinds.reduce((sum, kind) => sum + Math.max(0,
      (change.counts?.[kind] ?? Object.keys(change[kind] || []).length) - Object.keys(change[kind] || []).length), 0);
    const note = change.truncated || omitted ? `<p role="note">${omitted || 'Additional'} change details omitted in this category. Inspect the complete descriptors of both versions for those changes.</p>` : '';
    return `<article data-diff-category="${category}" class="hd-card"><h3>${esc(category[0].toUpperCase() + category.slice(1))}: ${change.changed ? 'Changed' : 'No change'}</h3><p>${esc(CATEGORY_HELP[category])}</p><p>${totals}</p>${note}${kinds.map(kind => {
      const items = change[kind] || [];
      return items.length || Object.keys(items).length ? `<details open><summary>${esc(kind)}</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${json(items)}</pre></details>` : '';
    }).join('')}</article>`;
  }).join('')}`;
}

async function renderReportReader(container, client, id, artifact, versions, { versionId, signal: outer } = {}) {
  const shell = document.createElement('div');
  shell.dataset.artifactDetail = id;
  // The app shell owns the "← Artifacts" back link.
  shell.innerHTML = '<div data-artifact-header></div><div data-artifact-controls></div><div data-artifact-body><herdr-spinner label="Loading report"></herdr-spinner></div><section class="hd-card" data-artifact-share-card><h2>Share</h2><div data-artifact-share></div></section>';
  container.replaceChildren(shell);
  const header = shell.querySelector('[data-artifact-header]');
  const controls = shell.querySelector('[data-artifact-controls]');
  const body = shell.querySelector('[data-artifact-body]');
  let disposed = false;
  let generation = 0;
  let selectedVersion = versionId;
  let controller = new AbortController();
  // Route cancellation (outer signal) disposes this reader and aborts its reads.
  const stop = () => { disposed = true; generation += 1; controller.abort(); revokeBlobs(); };
  if (outer?.aborted) disposed = true; else outer?.addEventListener('abort', stop, { once: true });
  const blobs = new Set();
  const alive = ticket => !disposed && ticket === generation && container.contains(shell);
  const revokeBlobs = () => { for (const url of blobs) URL.revokeObjectURL(url); blobs.clear(); };

  function showPreview(output, content) {
    const template = document.createElement('template');
    template.innerHTML = content;
    // Even fragment links in srcdoc can resolve against the parent SPA URL.
    // The surrounding reader owns navigation and authenticated downloads.
    template.content.querySelectorAll('a').forEach(link => link.replaceWith(...link.childNodes));
    const frame = document.createElement('iframe');
    frame.title = 'Report preview';
    frame.setAttribute('sandbox', '');
    frame.referrerPolicy = 'no-referrer';
    frame.style.cssText = 'width:100%;min-height:560px;border:1px solid currentColor';
    frame.srcdoc = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'">${template.innerHTML}`;
    const note = document.createElement('p');
    note.textContent = 'Use the surrounding reader controls to navigate findings and download files.';
    output.replaceChildren(note, frame);
  }

  function showLegacyVersion(version, selected, ticket, signal) {
    const outputs = version.output_manifest || [];
    body.innerHTML = `<p class="hd-note" data-report-selected>Immutable version: <strong>${esc(selected)}</strong></p><section class="hd-card" data-report-legacy><h2>Legacy report version</h2><p>This version preserves rendered files without structured findings or independent report checks.</p>${outputs.length ? outputs.map((item, index) => `<article><h3>${esc(item.name || `Output ${index + 1}`)}</h3><p>${esc(item.media_type || 'application/octet-stream')} · ${humanSize(item.size_bytes)}</p><button type="button" class="hd-btn" data-legacy-download="${index}">Download ${esc(item.name || `output ${index + 1}`)}</button>${item.media_type === 'text/html' && Number.isSafeInteger(item.size_bytes) && item.size_bytes <= 256 * 1024 ? `<button type="button" class="hd-btn" data-legacy-preview="${index}">Preview ${esc(item.name)}</button>` : ''}<div role="status" data-legacy-output-status></div></article>`).join('') : '<p>No rendered files are attached to this version.</p>'}</section>`;
    body.querySelectorAll('[data-legacy-download],[data-legacy-preview]').forEach(button => button.addEventListener('click', async () => {
      const preview = button.hasAttribute('data-legacy-preview');
      const index = Number(preview ? button.dataset.legacyPreview : button.dataset.legacyDownload);
      const asset = outputs[index];
      const output = button.parentElement.querySelector('[data-legacy-output-status]');
      button.disabled = true;
      output.textContent = preview ? 'Loading preview…' : 'Downloading…';
      try {
        const path = `${client.artifactReportPath(id, selected)}/outputs/${index}`;
        const data = await client.artifactReportContent(path, 'blob', signal);
        if (!alive(ticket)) return;
        if (preview) {
          if (data.size > 256 * 1024) throw new Error('This output exceeds the preview limit. Download it instead.');
          const content = await data.text();
          if (alive(ticket)) showPreview(output, content);
        } else {
          const url = URL.createObjectURL(data);
          blobs.add(url);
          const link = document.createElement('a');
          link.href = url;
          link.download = (asset.name || `output-${index + 1}`).split(/[\\/]/).pop();
          link.click();
          output.textContent = 'Download ready.';
        }
      } catch (error) { if (alive(ticket)) output.textContent = `Output unavailable: ${error.message || 'Request failed'}`; }
      finally { if (alive(ticket)) button.disabled = false; }
    }));
  }

  async function loadVersion(selected, { updateLocation = true } = {}) {
    controller.abort();
    controller = new AbortController();
    const ticket = ++generation;
    const signal = controller.signal;
    selectedVersion = selected;
    revokeBlobs();
    body.onclick = null;
    const selector = controls.querySelector('[data-report-version]');
    if (selector) selector.value = selected;
    body.innerHTML = '<herdr-spinner label="Loading report version"></herdr-spinner>';
    if (updateLocation && globalThis.location?.hash.startsWith('#/artifacts/')) {
      const route = location.hash.split('?')[0];
      history.replaceState(history.state, '', `${location.pathname}${location.search}${route}?version=${encodeURIComponent(selected)}`);
    }
    try {
      const summary = versions.find(version => version.version_id === selected);
      if (summary?.is_report !== true && !summary?.report_descriptor) {
        const version = await client.artifactVersion(id, selected, { signal });
        if (!alive(ticket)) return;
        if (version?.version_id !== selected || version?.artifact_id !== id) throw new Error('The server did not return the requested immutable version.');
        if (!version.report_descriptor) {
          showLegacyVersion(version, selected, ticket, signal);
          return;
        }
      }
      const report = await client.artifactReport(id, selected, { signal });
      if (!alive(ticket)) return;
      if (!report?.report_descriptor || report.version_id !== selected) throw new Error('The server did not return the requested report version.');
      const descriptor = report.report_descriptor;
      const assets = report.assets || descriptor.assets || [];
      const options = versions.filter(version => version.version_id !== selected && version.is_report !== false).map(version => `<option value="${esc(version.version_id)}">v${esc(version.version_number)} · ${esc(version.version_id)}</option>`).join('');
      body.innerHTML = `<p class="hd-note" data-report-selected>Immutable version: <strong>${esc(selected)}</strong> · ${esc(report.state || 'No review state available')}</p>${descriptorHtml(descriptor)}${checkHtml(report.checks)}<section class="hd-card"><h2>Meaningful version differences</h2><label>Compare against<select class="hd-input" data-report-diff-base><option value="">Choose another version</option>${options}</select></label><button type="button" class="hd-btn" data-report-compare>Compare versions</button><div role="status" data-report-diff-result></div></section>${feedbackHtml(report)}<section class="hd-card"><h2>Lightweight preview</h2><p>The preview contains bounded report content. Supporting datasets are downloaded separately.</p><button type="button" class="hd-btn" data-report-preview>Load lightweight preview</button><div role="status" data-report-preview-result></div></section>${assetHtml(assets)}`;

      // Delegation also covers feedback anchors appended by later pages.
      body.onclick = event => {
        const button = event.target.closest('[data-report-jump],[data-report-comment]');
        if (!button || !body.contains(button)) return;
        if (button.dataset.reportJump) {
          const [kind, target] = JSON.parse(button.dataset.reportJump);
          const item = [...body.querySelectorAll('[data-report-item]')].find(node => node.dataset.kind === kind && node.dataset.itemId === target);
          item?.scrollIntoView({ block: 'center', behavior: 'smooth' });
          item?.focus({ preventScroll: true });
        } else {
          body.querySelector('[data-report-feedback-target]').value = button.dataset.reportComment;
          body.querySelector('[data-report-feedback-body]').focus();
        }
      };
      body.querySelector('[data-report-compare]').addEventListener('click', async event => {
        const base = body.querySelector('[data-report-diff-base]').value;
        const output = body.querySelector('[data-report-diff-result]');
        if (!base) { output.textContent = 'Choose a version to compare.'; return; }
        event.currentTarget.disabled = true;
        output.textContent = 'Comparing pinned versions…';
        try {
          const diff = await client.artifactReportDiff(id, selected, base, { signal });
          if (alive(ticket)) output.innerHTML = renderReportDiff(diff, base, selected);
        } catch (error) { if (alive(ticket)) output.innerHTML = `<p role="alert">Comparison unavailable: ${failure(error)}</p>`; }
        finally { if (alive(ticket)) body.querySelector('[data-report-compare]').disabled = false; }
      });
      const moreFeedback = body.querySelector('[data-report-feedback-more]');
      if (moreFeedback) {
        let offset = (report.feedback || []).length;
        const seen = new Set((report.feedback || []).map(item => item.feedback_id));
        moreFeedback.addEventListener('click', async () => {
          moreFeedback.disabled = true;
          const output = body.querySelector('[data-report-feedback-more-status]');
          output.textContent = 'Loading more version-pinned feedback…';
          try {
            const page = await client.artifactReportFeedback(id, selected, { signal, limit: 20, offset });
            if (!alive(ticket)) return;
            if (page.version_id !== selected) throw new Error('Feedback belongs to a different version.');
            const rows = page.feedback || [];
            offset += rows.length;
            const fresh = rows.filter(item => !seen.has(item.feedback_id));
            fresh.forEach(item => seen.add(item.feedback_id));
            body.querySelector('[data-report-feedback-list]').insertAdjacentHTML('beforeend', feedbackRows(fresh));
            output.textContent = '';
            if (!page.has_more) moreFeedback.remove();
            else if (!rows.length) { output.textContent = 'No further feedback page was returned. Refresh this version to retry.'; }
            else moreFeedback.disabled = false;
          } catch (error) { if (alive(ticket)) { output.textContent = `Feedback could not load: ${error.message || 'Request failed'}`; moreFeedback.disabled = false; } }
        });
      }
      body.querySelector('[data-report-feedback-form]').addEventListener('submit', async event => {
        event.preventDefault();
        const form = event.currentTarget;
        const output = form.querySelector('[data-report-feedback-status]');
        const text = form.querySelector('[data-report-feedback-body]').value.trim();
        if (!text) { output.textContent = 'Write feedback before submitting.'; return; }
        const [target_kind, target_id] = JSON.parse(form.querySelector('[data-report-feedback-target]').value);
        form.querySelector('button[type="submit"]').disabled = true;
        try {
          await client.addArtifactReportFeedback(id, selected, { target_kind, target_id, body: text }, { signal });
          if (alive(ticket)) await loadVersion(selected, { updateLocation: false });
        } catch (error) {
          if (alive(ticket)) { output.innerHTML = `<span role="alert">Feedback could not be saved: ${failure(error)}</span>`; form.querySelector('button[type="submit"]').disabled = false; }
        }
      });
      body.querySelector('[data-report-check-form]').addEventListener('submit', async event => {
        event.preventDefault();
        const form = event.currentTarget;
        const output = form.querySelector('[data-report-check-result]');
        const stage = form.querySelector('[data-report-check-stage]').value;
        const status = form.querySelector('[data-report-check-status]').value;
        const notes = form.querySelector('[data-report-check-notes]').value.trim();
        const evidence_refs = [...form.querySelectorAll('[data-report-check-reference]:checked')].map(node => node.value);
        if (!notes || (stage !== 'rendered' && status === 'passed' && !evidence_refs.length)) {
          output.textContent = 'Describe the check and select supporting evidence before recording a pass.'; return;
        }
        form.querySelector('button[type="submit"]').disabled = true;
        try {
          await client.recordArtifactReportCheck(id, selected, { stage, status, notes, evidence_refs }, { signal });
          if (alive(ticket)) await loadVersion(selected, { updateLocation: false });
        } catch (error) {
          if (alive(ticket)) { output.innerHTML = `<span role="alert">Check could not be recorded: ${failure(error)}</span>`; form.querySelector('button[type="submit"]').disabled = false; }
        }
      });
      body.querySelector('[data-report-preview]').addEventListener('click', async event => {
        event.currentTarget.disabled = true;
        const output = body.querySelector('[data-report-preview-result]');
        output.textContent = 'Loading bounded preview…';
        try {
          const preview = await client.artifactReportPreview(id, selected, { signal });
          if (!alive(ticket)) return;
          if (typeof preview !== 'string' || new TextEncoder().encode(preview).length > 1024 * 1024) throw new Error('The preview exceeds its display size limit. Use the individual downloads.');
          showPreview(output, preview);
        } catch (error) {
          if (alive(ticket)) { output.innerHTML = `<p role="alert">Preview unavailable: ${failure(error)}</p>`; body.querySelector('[data-report-preview]').disabled = false; }
        }
      });
      body.querySelectorAll('[data-report-download]').forEach(button => button.addEventListener('click', async () => {
        const asset = assets.find(item => item.id === button.dataset.reportDownload);
        if (!asset) return;
        button.disabled = true;
        const output = button.parentElement.querySelector('[data-download-status]');
        output.textContent = ' Downloading…';
        try {
          const data = await client.downloadArtifactReportAsset(id, selected, asset.id, { signal });
          if (!alive(ticket)) return;
          const url = URL.createObjectURL(data);
          blobs.add(url);
          const link = document.createElement('a');
          link.href = url;
          link.download = (asset.name || asset.id).split(/[\\/]/).pop();
          link.click();
          output.textContent = ' Download ready.';
        } catch (error) { if (alive(ticket)) output.textContent = ` Download failed: ${error.message || 'Request failed'}`; }
        finally { if (alive(ticket)) button.disabled = false; }
      }));
    } catch (error) {
      if (alive(ticket)) body.innerHTML = `<section class="hd-card"><p role="alert">Report version could not load: ${failure(error)}</p><button type="button" class="hd-btn" data-report-retry>Retry this version</button></section>`;
      if (alive(ticket)) body.querySelector('[data-report-retry]')?.addEventListener('click', () => loadVersion(selected, { updateLocation: false }));
    }
  }

  // The artifact and its version summaries were read by the caller.
  const errors = [];
  // The Share card lives outside the per-version body (replaced on every version switch); the route signal disposes it.
  // Its errors stay inside the card and never reach the route's freshness errors.
  if (!disposed) mountSharePanel(shell.querySelector('[data-artifact-share]'), client, id, { versions, headVersionId: artifact.current_version_id, signal: outer, allowEntry: false });
  try {
    header.innerHTML = `<h2>${esc(artifact.display_name || artifact.original_filename || id)}</h2><p class="hd-note">Report artifact ${esc(id)}</p>`;
    if (!versions.length) { body.innerHTML = '<p>No immutable report version has been committed yet.</p>'; return { errors }; }
    selectedVersion = selectedVersion || artifact.current_version_id || versions[0].version_id;
    if (!versions.some(version => version.version_id === selectedVersion)) throw new Error('The requested immutable version is unavailable.');
    controls.innerHTML = `<section class="hd-card"><label>Report version<select class="hd-input" data-report-version>${versions.map(version => `<option value="${esc(version.version_id)}">v${esc(version.version_number)} · ${esc(version.version_id)}</option>`).join('')}</select></label><p>All findings, checks, feedback and downloads below refer to the selected immutable version.</p></section>`;
    controls.querySelector('[data-report-version]').addEventListener('change', event => { void loadVersion(event.target.value); });
    await loadVersion(selectedVersion, { updateLocation: false });
  } catch (error) {
    errors.push(`Artifact could not load: ${error.message || 'request failed'}`);
    if (!disposed && container.contains(shell)) body.innerHTML = `<p role="alert">Artifact could not load: ${failure(error)}</p>`;
  }
  return { errors };
}

export async function renderArtifactDetail(container, client, id, { signal, versionId } = {}) {
  // Each read has its own deadline; the caller's signal only represents route cancellation.
  const bounded = () => signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000);
  const artifact = await client.getArtifact(id, { signal: bounded() });
  if (signal?.aborted || !container.isConnected) return;
  const declaresReport = versions => artifact.artifact_type === 'report' || versions.some(version => version.is_report === true || version.report_descriptor);
  // Version summaries are small (no descriptors) and decide which reader opens.
  const versionsRead = client.artifactVersions(id, { signal: bounded(), summary: true });
  if (artifact.artifact_type === 'report') {
    // A report is always read through its version history; a failure is the caller's visible error.
    const { versions } = await versionsRead;
    if (signal?.aborted || !container.isConnected) return;
    return renderReportReader(container, client, id, artifact, Array.isArray(versions) ? versions : [], { signal, versionId });
  }
  versionsRead.catch(() => {});
  const mime = artifact.mime_type || 'application/octet-stream';
  const url = client.artifactDownloadUrl(id);
  container.innerHTML = `<h2>${esc(artifact.display_name || artifact.original_filename || id)}</h2><div class="hd-card"><h2>Preview</h2><div data-artifact-preview>${skeleton('preview', { rows: 1 })}</div></div><div class="hd-card"><dl><dt>Artifact ID</dt><dd><code>${esc(id)}</code></dd><dt>Type</dt><dd>${esc(artifact.artifact_type || 'other')}</dd><dt>MIME</dt><dd>${esc(mime)}</dd><dt>Status</dt><dd>${esc(artifact.status || 'unknown')}</dd><dt>SHA-256</dt><dd><code>${esc(artifact.sha256 || 'versioned blob')}</code></dd></dl></div><div class="hd-card"><h2>Versions</h2><div data-artifact-versions>${skeleton('versions', { rows: 1 })}</div></div><div class="hd-card" data-artifact-share-card><h2>Share</h2><div data-artifact-share></div></div><div class="hd-card"><h2>Review</h2><div data-artifact-reviews>${skeleton('reviews', { rows: 1 })}</div></div><div class="hd-card"><h2>Linked to</h2><div data-artifact-links>${skeleton('links', { rows: 1 })}</div></div>`;
  client.artifactLinks(id, { signal: bounded() }).then(({ links }) => {
    const section = container.querySelector('[data-artifact-links]');
    if (!section || signal?.aborted) return;
    section.innerHTML = Array.isArray(links) && links.length
      ? `<ul>${links.map(link => `<li><strong>${esc(link.link_kind || 'reference')}</strong> · ${esc(link.project_id ? `project ${link.project_id}` : link.issue_ref ? `issue ${link.issue_ref}` : link.task_id ? `task ${link.task_id}` : link.room_id ? `room ${link.room_id}` : 'unknown')}</li>`).join('')}</ul>`
      : '<herdr-empty message="Not linked to any project, issue, task or room"></herdr-empty>';
  }).catch(error => {
    const section = container.querySelector('[data-artifact-links]');
    if (section && !signal?.aborted) section.innerHTML = `<herdr-error message="Links unavailable: ${esc(error?.message || 'request failed')}"></herdr-error>`;
  });
  const reviewsRead = client.artifactReviews(id, { signal: bounded() });
  reviewsRead.catch(() => {});
  const results = [(await Promise.allSettled([versionsRead]))[0]];
  if (signal?.aborted || !container.isConnected) return;
  // A logical artifact whose history declares reports opens the report reader without fetching its download.
  if (results[0].status === 'fulfilled' && Array.isArray(results[0].value?.versions) && declaresReport(results[0].value.versions)) {
    return renderReportReader(container, client, id, artifact, results[0].value.versions, { signal, versionId });
  }
  // Share card: its own list read and errors (kept out of the route's errors); the route signal disposes it.
  const share = mountSharePanel(container.querySelector('[data-artifact-share]'), client, id, {
    versions: results[0].status === 'fulfilled' && Array.isArray(results[0].value?.versions) ? results[0].value.versions : [],
    headVersionId: artifact.current_version_id, signal,
  });
  // Only a non-report artifact may fetch the download for its preview.
  container.querySelector('[data-artifact-preview]').innerHTML = mime === 'application/pdf' ? `<iframe title="PDF preview" src="${esc(url)}" style="width:100%;min-height:640px;border:0"></iframe>` : mime.startsWith('image/') ? `<img src="${esc(url)}" alt="${esc(artifact.original_filename || id)}" style="max-width:100%;max-height:640px;object-fit:contain">` : mime === 'text/html' ? `<iframe title="HTML preview" sandbox src="${esc(url)}" style="width:100%;min-height:640px;border:0"></iframe>` : mime.startsWith('text/') || mime === 'application/json' ? `<a class="hd-btn" href="${esc(url)}" target="_blank" rel="noopener">Open text artifact</a>` : `<a class="hd-btn" href="${esc(url)}" download>Download artifact</a>`;
  results.push((await Promise.allSettled([reviewsRead]))[0]);
  if (signal?.aborted || !container.isConnected) return;
  const errors = [];
  for (const [index, key] of ['versions', 'reviews'].entries()) {
    const result = results[index];
    const section = container.querySelector(`[data-artifact-${key}]`);
    const items = result.status === 'fulfilled' ? result.value?.[key] : null;
    if (!Array.isArray(items)) {
      const message = `${key === 'versions' ? 'Versions' : 'Reviews'} unavailable: ${result.status === 'rejected' ? result.reason?.message || 'request failed' : 'Invalid response'}`;
      errors.push(message);
      section.innerHTML = `<herdr-error message="${esc(message)}"></herdr-error><button type="button" data-route-retry>Retry</button>`;
    } else if (!items.length) {
      section.innerHTML = `<herdr-empty message="${key === 'versions' ? 'No version history available' : 'No review requested'}"></herdr-empty>`;
    } else if (key === 'versions') {
      // Restore re-reads the list and feeds the Share panel the same versions and head; the route signal disposes both.
      mountHistory(section, client, id, {
        versions: items, headVersionId: artifact.current_version_id, canRestore: true, signal,
        onVersions: (nextVersions, nextHead) => share.setVersions(nextVersions, nextHead),
      });
    } else {
      section.innerHTML = `<ul>${items.map(item => `<li><strong>${esc(item.state)}</strong> · ${esc(item.version_id)} · ${esc(item.reviewer || item.requested_by)}</li>`).join('')}</ul>`;
    }
  }
  return { errors };
}
