import { escapeHtml as esc } from './herdr-web-components/util.js';

export function mountPublications(container, client) {
  let disposed = false;
  const render = async () => {
    container.innerHTML = '<herdr-spinner label="Loading publications"></herdr-spinner>';
    try {
      const { publications = [] } = await client.listPublications();
      if (disposed) return;
      container.innerHTML = `<div class="hd-card"><div class="hd-card__title">Publications</div><p class="hd-card__sub">External access is explicit, pinned, expiring, and revocable.</p>${publications.length ? `<div class="hd-table-wrap"><table class="hd-table"><thead><tr><th>Resource</th><th>Pinned version</th><th>Audience</th><th>State</th><th>Expiry</th><th>Actions</th></tr></thead><tbody>${publications.map(publication => `<tr><td>${esc(publication.resource_id)}</td><td><code>${esc(publication.pinned_version_id)}</code></td><td>${esc(publication.audience)}</td><td>${esc(publication.state || publication.desired_state || 'unknown')}</td><td>${esc(publication.expires_at || 'unknown')}</td><td>${publication.state === 'requested' ? `<button class="hd-btn hd-btn--sm" data-approve-publication="${esc(publication.publication_id)}">Approve</button>` : ''}${publication.state === 'active' ? `<button class="hd-btn hd-btn--danger hd-btn--sm" data-revoke-publication="${esc(publication.publication_id)}">Revoke</button>` : ''}</td></tr>`).join('')}</tbody></table></div>` : '<herdr-empty message="No publications requested"></herdr-empty>'}</div>`;
      container.querySelectorAll('[data-approve-publication]').forEach(button => button.addEventListener('click', async () => { button.disabled = true; await client.approvePublication(button.dataset.approvePublication); await render(); }));
      container.querySelectorAll('[data-revoke-publication]').forEach(button => button.addEventListener('click', async () => { button.disabled = true; await client.revokePublication(button.dataset.revokePublication); await render(); }));
    } catch (error) {
      if (!disposed) container.innerHTML = `<herdr-error message="${esc(error.message || 'Publications could not load')}"></herdr-error>`;
    }
  };
  void render();
  return () => { disposed = true; };
}
