import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';
import { morph } from './patch.js';
import { pollWhileVisible } from './refresh-bus.js';

export function mountPublications(container, client, { freshness = () => null } = {}) {
  let disposed = false, controller = null, loaded = false, acting = false;
  container.innerHTML = '<p data-publication-state role="status"></p><div data-publication-results></div><button type="button" data-publication-retry hidden>Retry</button>';
  const state = container.querySelector('[data-publication-state]');
  const results = container.querySelector('[data-publication-results]');
  const retry = container.querySelector('[data-publication-retry]');
  const render = async () => {
    if (disposed || controller || acting) return;
    const request = new AbortController(); controller = request;
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(15000)]);
    if (!loaded) results.innerHTML = skeleton('publications');
    retry.hidden = true;
    try {
      const { publications } = await client.listPublications({ signal });
      if (!Array.isArray(publications)) throw new Error('Invalid publications response');
      if (disposed) return;
      morph(results, `<div class="hd-card"><div class="hd-card__title">Publications</div><p class="hd-card__sub">External access is explicit, pinned, expiring, and revocable.</p>${publications.length ? `<div class="hd-table-wrap"><table class="hd-table"><thead><tr><th>Resource</th><th>Pinned version</th><th>Audience</th><th>State</th><th>Expiry</th><th>Actions</th></tr></thead><tbody>${publications.map(publication => `<tr><td>${esc(publication.resource_id)}</td><td><code>${esc(publication.pinned_version_id)}</code></td><td>${esc(publication.audience)}</td><td>${esc(publication.state || publication.desired_state || 'unknown')}</td><td>${esc(publication.expires_at || 'unknown')}</td><td>${publication.state === 'requested' ? `<button class="hd-btn hd-btn--sm" data-approve-publication="${esc(publication.publication_id)}">Approve</button>` : ''}${publication.state === 'active' ? `<button class="hd-btn hd-btn--danger hd-btn--sm" data-revoke-publication="${esc(publication.publication_id)}">Revoke</button>` : ''}</td></tr>`).join('')}</tbody></table></div>` : '<herdr-empty message="No publications requested"></herdr-empty>'}</div>`);
      loaded = true;
      state.textContent = '';
      freshness()?.touch(true);
    } catch (error) {
      if (!disposed && !request.signal.aborted) {
        if (!loaded) results.innerHTML = '';
        state.textContent = `Publications could not load: ${error.message}`;
        retry.hidden = false;
        freshness()?.touch(false, error.message);
      }
    } finally { if (controller === request) controller = null; }
  };
  const act = async event => {
    const button = event.target.closest('[data-approve-publication],[data-revoke-publication]');
    if (!button || acting || disposed) return;
    acting = true;
    controller?.abort();
    const request = new AbortController(); controller = request;
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(15000)]);
    button.disabled = true;
    state.textContent = button.dataset.approvePublication ? 'Approving publication…' : 'Revoking publication…';
    try {
      if (button.dataset.approvePublication) await client.approvePublication(button.dataset.approvePublication, { signal });
      else await client.revokePublication(button.dataset.revokePublication, { signal });
      if (disposed) return;
      acting = false; controller = null;
      await render();
    } catch (error) {
      if (!disposed) { state.textContent = `Publication action failed: ${error.message}. Retry the action.`; freshness()?.touch(false, error.message); }
    } finally { acting = false; if (controller === request) controller = null; if (!disposed) button.disabled = false; }
  };
  container.addEventListener('click', act);
  retry.addEventListener('click', render);
  const stop = pollWhileVisible(render, 30000);
  void render();
  return { refresh: render, dispose() { disposed = true; stop(); controller?.abort(); container.removeEventListener('click', act); } };
}
