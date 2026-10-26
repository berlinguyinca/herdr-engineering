import { escapeHtml as esc } from './herdr-web-components/util.js';

const humanSize = value => {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 ** 2).toFixed(1)} MB`;
};

export function mountArtifacts(container, client) {
  let disposed = false;
  let timer;
  const render = async (filters = {}) => {
    const existing = container.querySelector('[data-artifact-registry]');
    if (!existing) {
      container.innerHTML = '<div class="hd-card" data-artifact-registry><div class="hd-card__title">Artifact registry</div><div class="hd-inline"><input class="hd-input" data-artifact-query placeholder="Search artifacts" aria-label="Search artifacts"><select class="hd-input" data-artifact-type aria-label="Filter artifact type"><option value="">All types</option><option value="other">Other</option><option value="web">Web</option><option value="document">Document</option></select><select class="hd-input" data-artifact-status aria-label="Filter artifact status"><option value="">All statuses</option><option value="available">Available</option><option value="draft">Draft</option></select></div><div data-artifact-results><herdr-spinner label="Loading artifacts"></herdr-spinner></div></div>';
      const schedule = () => { clearTimeout(timer); timer = setTimeout(() => render({
        q: container.querySelector('[data-artifact-query]').value,
        type: container.querySelector('[data-artifact-type]').value,
        status: container.querySelector('[data-artifact-status]').value,
      }), 200); };
      container.querySelectorAll('[data-artifact-query],[data-artifact-type],[data-artifact-status]').forEach(control => control.addEventListener('input', schedule));
    }
    const results = container.querySelector('[data-artifact-results]');
    results.innerHTML = '<herdr-spinner label="Loading artifacts"></herdr-spinner>';
    try {
      const { artifacts = [] } = await client.listArtifacts(filters);
      if (disposed) return;
      results.innerHTML = `<p class="hd-card__sub">Durable artifacts and provenance from the shared fabric.</p>${artifacts.length ? `<div class="hd-table-wrap"><table class="hd-table"><thead><tr><th>Name</th><th>Type</th><th>Size</th><th>Status</th><th>Created</th></tr></thead><tbody>${artifacts.map(artifact => `<tr><td><a href="#/artifacts/${encodeURIComponent(artifact.artifact_id)}">${esc(artifact.display_name || artifact.original_filename || artifact.artifact_id)}</a></td><td>${esc(artifact.artifact_type || 'other')}</td><td>${humanSize(artifact.size_bytes)}</td><td>${esc(artifact.status || 'unknown')}</td><td>${esc(artifact.created_at || 'unknown')}</td></tr>`).join('')}</tbody></table></div>` : '<herdr-empty message="No artifacts match the current filters"></herdr-empty>'}`;
    } catch (error) {
      if (!disposed) results.innerHTML = `<herdr-error message="${esc(error.message || 'Artifacts could not load')}"></herdr-error>`;
    }
  };
  void render();
  return () => { disposed = true; };
}

export async function renderArtifactDetail(container, client, id) {
  try {
    const artifact = await client.getArtifact(id);
    const [versions, reviews] = await Promise.all([
      client.artifactVersions(id).catch(() => ({ versions: [] })),
      client.artifactReviews(id).catch(() => ({ reviews: [] })),
    ]);
    const mime = artifact.mime_type || 'application/octet-stream';
    const url = client.artifactDownloadUrl(id);
    const preview = mime === 'application/pdf' ? `<iframe title="PDF preview" src="${esc(url)}" style="width:100%;min-height:640px;border:0"></iframe>` : mime.startsWith('image/') ? `<img src="${esc(url)}" alt="${esc(artifact.original_filename || id)}" style="max-width:100%;max-height:640px;object-fit:contain">` : mime === 'text/html' ? `<iframe title="HTML preview" sandbox src="${esc(url)}" style="width:100%;min-height:640px;border:0"></iframe>` : mime.startsWith('text/') || mime === 'application/json' ? `<a class="hd-btn" href="${esc(url)}" target="_blank" rel="noopener">Open text artifact</a>` : `<a class="hd-btn" href="${esc(url)}" download>Download artifact</a>`;
    container.innerHTML = `<a href="#/artifacts">← Artifacts</a><h1>${esc(artifact.display_name || artifact.original_filename || id)}</h1><div class="hd-card"><h2>Preview</h2>${preview}</div><div class="hd-card"><dl><dt>Artifact ID</dt><dd><code>${esc(id)}</code></dd><dt>Type</dt><dd>${esc(artifact.artifact_type || 'other')}</dd><dt>MIME</dt><dd>${esc(mime)}</dd><dt>Status</dt><dd>${esc(artifact.status || 'unknown')}</dd><dt>SHA-256</dt><dd><code>${esc(artifact.sha256 || 'versioned blob')}</code></dd></dl></div><div class="hd-card"><h2>Versions</h2>${(versions.versions || []).length ? `<ul>${versions.versions.map(version => `<li><strong>v${esc(version.version_number)}</strong> · ${esc(version.state || 'unknown')} · ${esc(version.version_id)}</li>`).join('')}</ul>` : '<herdr-empty message="No version history available"></herdr-empty>'}</div><div class="hd-card"><h2>Review</h2>${(reviews.reviews || []).length ? `<ul>${reviews.reviews.map(review => `<li><strong>${esc(review.state)}</strong> · ${esc(review.version_id)} · ${esc(review.reviewer || review.requested_by)}</li>`).join('')}</ul>` : '<herdr-empty message="No review requested"></herdr-empty>'}</div>`;
  } catch (error) {
    container.innerHTML = `<herdr-error message="${esc(error.message || 'Artifact could not load')}"></herdr-error>`;
  }
}
