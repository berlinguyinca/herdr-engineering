import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';
import { morph } from './patch.js';
import { pollWhileVisible } from './refresh-bus.js';

const humanSize = value => {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 ** 2).toFixed(1)} MB`;
};

export function mountArtifacts(container, client, { freshness = () => null } = {}) {
  let disposed = false, timer, controller, generation = 0, loaded = false;
  container.innerHTML = '<div class="hd-card" data-artifact-registry><div class="hd-card__title">Artifact registry</div><div class="hd-inline"><input class="hd-input" data-artifact-query placeholder="Search artifacts" aria-label="Search artifacts"><select class="hd-input" data-artifact-type aria-label="Filter artifact type"><option value="">All types</option><option value="other">Other</option><option value="web">Web</option><option value="document">Document</option></select><select class="hd-input" data-artifact-status aria-label="Filter artifact status"><option value="">All statuses</option><option value="available">Available</option><option value="draft">Draft</option></select></div><p data-artifact-state role="status"></p><div data-artifact-results></div><button type="button" data-artifact-retry hidden>Retry</button></div>';
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
      const { artifacts } = await client.listArtifacts({
        q: container.querySelector('[data-artifact-query]').value,
        type: container.querySelector('[data-artifact-type]').value,
        status: container.querySelector('[data-artifact-status]').value, limit: 100, signal,
      });
      if (disposed || current !== generation) return;
      if (!Array.isArray(artifacts)) throw new Error('Invalid artifact registry response');
      morph(results, artifacts.length ? `<div class="hd-table-wrap"><table class="hd-table"><thead><tr><th>Name</th><th>Type</th><th>Size</th><th>Status</th><th>Created</th></tr></thead><tbody>${artifacts.map(artifact => `<tr><td><a href="#/artifacts/${encodeURIComponent(artifact.artifact_id)}">${esc(artifact.display_name || artifact.original_filename || artifact.artifact_id)}</a></td><td>${esc(artifact.artifact_type || 'other')}</td><td>${humanSize(artifact.size_bytes)}</td><td>${esc(artifact.status || 'unknown')}</td><td>${esc(artifact.created_at || 'unknown')}</td></tr>`).join('')}</tbody></table></div>` : '<herdr-empty message="No artifacts match the current filters"></herdr-empty>');
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
  container.querySelectorAll('[data-artifact-query],[data-artifact-type],[data-artifact-status]').forEach(control => control.addEventListener('input', schedule));
  retry.addEventListener('click', render);
  const stop = pollWhileVisible(render, 30000);
  void render();
  return { refresh: render, dispose() { disposed = true; clearTimeout(timer); stop(); controller?.abort(); } };
}

export async function renderArtifactDetail(container, client, id, { signal } = {}) {
  // Each read has its own deadline; the caller's signal only represents route cancellation.
  const bounded = () => signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000);
  const artifact = await client.getArtifact(id, { signal: bounded() });
  if (signal?.aborted || !container.isConnected) return;
  const mime = artifact.mime_type || 'application/octet-stream';
  const url = client.artifactDownloadUrl(id);
  const preview = mime === 'application/pdf' ? `<iframe title="PDF preview" src="${esc(url)}" style="width:100%;min-height:640px;border:0"></iframe>` : mime.startsWith('image/') ? `<img src="${esc(url)}" alt="${esc(artifact.original_filename || id)}" style="max-width:100%;max-height:640px;object-fit:contain">` : mime === 'text/html' ? `<iframe title="HTML preview" sandbox src="${esc(url)}" style="width:100%;min-height:640px;border:0"></iframe>` : mime.startsWith('text/') || mime === 'application/json' ? `<a class="hd-btn" href="${esc(url)}" target="_blank" rel="noopener">Open text artifact</a>` : `<a class="hd-btn" href="${esc(url)}" download>Download artifact</a>`;
  container.innerHTML = `<h2>${esc(artifact.display_name || artifact.original_filename || id)}</h2><div class="hd-card"><h2>Preview</h2>${preview}</div><div class="hd-card"><dl><dt>Artifact ID</dt><dd><code>${esc(id)}</code></dd><dt>Type</dt><dd>${esc(artifact.artifact_type || 'other')}</dd><dt>MIME</dt><dd>${esc(mime)}</dd><dt>Status</dt><dd>${esc(artifact.status || 'unknown')}</dd><dt>SHA-256</dt><dd><code>${esc(artifact.sha256 || 'versioned blob')}</code></dd></dl></div><div class="hd-card"><h2>Versions</h2><div data-artifact-versions>${skeleton('versions', { rows: 1 })}</div></div><div class="hd-card"><h2>Review</h2><div data-artifact-reviews>${skeleton('reviews', { rows: 1 })}</div></div>`;
  const results = await Promise.allSettled([
    client.artifactVersions(id, { signal: bounded() }),
    client.artifactReviews(id, { signal: bounded() }),
  ]);
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
    } else {
      section.innerHTML = `<ul>${items.map(item => key === 'versions'
        ? `<li><strong>v${esc(item.version_number)}</strong> · ${esc(item.state || 'unknown')} · ${esc(item.version_id)}</li>`
        : `<li><strong>${esc(item.state)}</strong> · ${esc(item.version_id)} · ${esc(item.reviewer || item.requested_by)}</li>`).join('')}</ul>`;
    }
  }
  return { errors };
}
