// Operator "Members" panel: member-owned artifacts, usage, limits, public links and purge (plan 5, Task 10).
//
// Pure renderers return HTML built only from esc()-escaped values; mountMembersPanel binds them to fixed regions.
// The section stays hidden unless the members list loads: a 403 (not an operator) or an unconfigured server hides it.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const MIB = 1024 * 1024;
const READ_MS = 15000;
const HIDING_STATUSES = new Set([401, 403, 404, 503]);
const PURGEABLE = new Set(['retracted', 'tombstoned']);
const LIMITS = [
  ['max_artifact_bytes', 'Max per artifact (MiB)', 'MiB'],
  ['max_member_bytes', 'Max per member (MiB)', 'MiB'],
  ['max_artifacts', 'Max artifacts', 'count'],
];
export const DISABLE_CONFIRM = 'Disable sharing for this member? Every live link they created (public ones included) is revoked now, and they cannot create new links until you enable sharing again.';

export function humanBytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < MIB) return `${(size / 1024).toFixed(1)} KiB`;
  if (size < 1024 * MIB) return `${(size / MIB).toFixed(1)} MiB`;
  return `${(size / (1024 * MIB)).toFixed(1)} GiB`;
}

export function bytesToMib(bytes) {
  if (bytes === null || bytes === undefined || bytes === '') return '';
  return String(Number((Number(bytes) / MIB).toFixed(3)));
}

/** Empty means unlimited (null); anything else must be a positive number of MiB. */
export function mibToBytes(text) {
  const trimmed = String(text ?? '').trim();
  if (trimmed === '') return null;
  const value = Number(trimmed);
  const bytes = Math.round(value * MIB);
  if (!Number.isFinite(value) || bytes <= 0 || bytes > Number.MAX_SAFE_INTEGER) throw new Error('Enter a positive number of MiB, or leave it empty for unlimited.');
  return bytes;
}

function countLimit(text) {
  const trimmed = String(text ?? '').trim();
  if (trimmed === '') return null;
  const value = Number(trimmed);
  if (!Number.isInteger(value) || value <= 0 || value > 2 ** 31 - 1) throw new Error('Enter a positive whole number, or leave it empty for unlimited.');
  return value;
}

const day = value => (value ? String(value).slice(0, 10) : '');
const nameOf = usage => usage.display_name || usage.member_principal || '';
const effective = usage => usage.limits || {};

/** A bar only when the limit is set; otherwise the word "unlimited". */
function usageBar(label, used, limit, format) {
  if (limit === null || limit === undefined) return `<div class="hd-meter-line">${esc(label)}: ${esc(format(used))}, unlimited</div>`;
  const shown = Math.min(Number(used || 0), Number(limit));
  return `<div class="hd-meter-line">${esc(label)}: ${esc(format(used))} of ${esc(format(limit))} `
    + `<progress value="${esc(shown)}" max="${esc(limit)}" aria-label="${esc(label)}"></progress></div>`;
}

const plural = (n, word) => `${n} ${word}${Number(n) === 1 ? '' : 's'}`;

function usageSummary(usage) {
  const limits = effective(usage);
  return usageBar('Stored', usage.bytes_used, limits.max_member_bytes, humanBytes)
    + `<div>Deleted, not purged: ${esc(humanBytes(usage.bytes_retracted))}</div>`
    + usageBar('Artifacts', usage.artifact_count, limits.max_artifacts, String)
    + `<div>${esc(plural(usage.artifact_count ?? 0, 'artifact'))}, ${esc(plural(usage.version_count ?? 0, 'version'))}</div>`
    + (limits.max_artifact_bytes ? `<div>Per artifact: up to ${esc(humanBytes(limits.max_artifact_bytes))}</div>` : '<div>Per artifact: unlimited</div>');
}

export function renderMemberRow(usage) {
  const live = usage.live_links || {};
  return `<tr data-member-row><td><button type="button" class="hd-btn" data-members-action="select" data-member="${esc(usage.member_principal)}">${esc(nameOf(usage))}</button>`
    + `${usage.sharing_disabled ? ' <span class="hd-badge" data-sharing-disabled>Sharing disabled</span>' : ''}</td>`
    + `<td>${usageSummary(usage)}</td>`
    + `<td>${esc(live.lab ?? 0)} group, ${esc(live.public ?? 0)} public</td>`
    + `<td>${esc(day(usage.last_used_at))}</td></tr>`;
}

export function renderMembersTable(members) {
  if (!members.length) return '<p>No member artifacts yet</p>';
  return '<div class="hd-table-wrap"><table class="hd-table"><thead><tr><th>Member</th><th>Usage</th><th>Live links</th><th>Last active</th></tr></thead>'
    + `<tbody>${members.map(renderMemberRow).join('')}</tbody></table></div>`;
}

export function renderMemberLinks(links, { empty = 'No links' } = {}) {
  if (!links.length) return `<p>${esc(empty)}</p>`;
  const rows = links.map(row => `<tr><td>${esc(row.created_by)}</td>`
    + `<td><a href="#/artifacts/${encodeURIComponent(row.artifact_id ?? '')}">${esc(row.title || row.artifact_id)}</a></td>`
    + `<td>${esc(row.audience)}</td><td>${esc(day(row.created_at))}</td><td>${row.expires_at ? esc(day(row.expires_at)) : 'no expiry'}</td>`
    + `<td><button type="button" class="hd-btn hd-btn--danger" data-members-action="revoke" data-member="${esc(row.created_by)}" data-share-id="${esc(row.share_id)}">Revoke</button></td></tr>`);
  return '<div class="hd-table-wrap"><table class="hd-table"><thead><tr><th>Member</th><th>Artifact</th><th>Audience</th><th>Created</th><th>Expires</th><th></th></tr></thead>'
    + `<tbody>${rows.join('')}</tbody></table></div>`;
}

function limitsForm(usage) {
  const inputs = LIMITS.map(([name, label, unit]) => {
    const current = usage[name];
    const value = current === null || current === undefined ? '' : (unit === 'MiB' ? bytesToMib(current) : String(current));
    return `<label>${esc(label)} <input class="hd-input" data-limit="${name}" type="text" inputmode="decimal" placeholder="unlimited" `
      + `aria-label="${esc(label)}" data-initial="${esc(value)}"${value === '' ? '' : ` value="${esc(value)}"`}></label>`;
  }).join(' ');
  return `<form data-members-limits><div class="hd-inline">${inputs} <button type="submit" class="hd-btn">Save limits</button></div>`
    + '<p class="hd-hint">Empty clears the override. Sizes are in MiB.</p></form>';
}

function artifactRows(member, artifacts) {
  if (!artifacts.length) return '<p>No artifacts</p>';
  const rows = artifacts.map(a => `<tr><td>${esc(a.title || a.artifact_id)}<div class="hd-hint">${esc(a.artifact_id)}</div></td>`
    + `<td>${esc(a.lifecycle)}</td><td>${esc(humanBytes(a.bytes))}</td><td>`
    + (PURGEABLE.has(a.lifecycle)
      ? `<button type="button" class="hd-btn hd-btn--danger" data-members-action="purge" data-member="${esc(member)}" data-artifact-id="${esc(a.artifact_id)}">Purge</button>`
      : '')
    + '</td></tr>');
  return `<div class="hd-table-wrap"><table class="hd-table"><thead><tr><th>Artifact</th><th>State</th><th>Size</th><th></th></tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}

export function renderMemberDetail(detail) {
  const usage = detail.usage || {};
  const sharing = usage.sharing_disabled
    ? '<span class="hd-badge" data-sharing-disabled>Sharing disabled</span> <button type="button" class="hd-btn" data-members-action="enable-sharing">Enable sharing</button>'
    : '<button type="button" class="hd-btn hd-btn--danger" data-members-action="disable-sharing">Disable sharing</button>';
  return `<div class="hd-card__title">${esc(nameOf({ ...usage, member_principal: detail.member }))} <span class="hd-hint">${esc(detail.member)}</span></div>`
    + `<div data-members-usage>${usageSummary(usage)}</div>`
    + `<div class="hd-inline" data-members-sharing>${sharing}</div>`
    + limitsForm(usage)
    + `<h4>Artifacts</h4>${artifactRows(detail.member, detail.artifacts || [])}`
    + '<div data-members-purge></div>'
    + `<h4>Links</h4>${renderMemberLinks(detail.links || [], { empty: 'No links' })}`;
}

function purgeForm(member, artifactId) {
  return `<form data-members-purge-form data-member="${esc(member)}" data-artifact-id="${esc(artifactId)}" class="hd-inline">`
    + `<label>Type <code>${esc(artifactId)}</code> to purge it for good: <input class="hd-input" data-purge-confirm type="text" autocomplete="off" aria-label="Type the artifact id to confirm"></label> `
    + '<button type="submit" class="hd-btn hd-btn--danger" disabled>Purge permanently</button> '
    + '<button type="button" class="hd-btn" data-members-action="purge-cancel">Cancel</button></form>';
}

const SHELL = '<div class="hd-card" data-members-panel><div class="hd-card__title">Members</div>'
  + '<p data-members-state role="status"></p><div data-members-list></div>'
  + '<div data-members-detail></div>'
  + '<h3>Public links by members</h3>'
  + '<label>Audience <select class="hd-input" data-members-audience aria-label="Link audience"><option value="public">Public</option><option value="lab">Group (lab)</option><option value="">All</option></select></label>'
  + '<div data-members-links></div></div>';

export function mountMembersPanel(container, client, { signal } = {}) {
  let disposed = false, listed = false, selected = null, busy = false;
  const stop = new AbortController();
  container.hidden = true;
  container.innerHTML = SHELL;
  const $ = selector => container.querySelector(selector);
  const status = text => { $('[data-members-state]').textContent = text; };
  const read = () => AbortSignal.any([stop.signal, AbortSignal.timeout(READ_MS), ...(signal ? [signal] : [])]);
  const alive = () => !disposed && !signal?.aborted;

  async function loadLinks() {
    const audience = $('[data-members-audience]').value;
    const { links } = await client.listArtifactMemberLinks({ audience: audience || undefined, state: 'live', signal: read() });
    if (alive()) $('[data-members-links]').innerHTML = renderMemberLinks(links || [], { empty: 'No live links' });
  }
  async function loadDetail() {
    if (!selected) { $('[data-members-detail]').innerHTML = ''; return; }
    const detail = await client.getArtifactMember(selected, { signal: read() });
    if (alive() && selected === detail.member) $('[data-members-detail]').innerHTML = renderMemberDetail(detail);
  }
  async function load() {
    try {
      const { members } = await client.listArtifactMembers({ signal: read() });
      if (!alive()) return;
      listed = true;
      container.hidden = false;
      $('[data-members-list]').innerHTML = renderMembersTable(members || []);
      status('');
      await Promise.all([loadLinks(), loadDetail()]);
    } catch (error) {
      if (!alive() || error?.name === 'AbortError') return;
      if (!listed && HIDING_STATUSES.has(error?.status)) { container.hidden = true; return; }
      container.hidden = false;
      status(`Members could not load: ${error?.message || 'request failed'}`);
    }
  }
  // One mutation at a time; the panel then re-reads everything so the screen shows server state, never a guess.
  async function mutate(action, message) {
    if (busy) return;
    busy = true;
    status('');
    try {
      await action();
      if (message) status(message);
      const keep = $('[data-members-state]').textContent;
      await load();
      if (keep) status(keep);
    } catch (error) {
      if (alive()) status(error?.message || 'The request could not be completed.');
    } finally { busy = false; }
  }

  const onClick = event => {
    const button = event.target.closest?.('[data-members-action]');
    if (!button || button.disabled) return;
    const member = button.dataset.member || selected;
    switch (button.dataset.membersAction) {
      case 'select': selected = member; void mutate(async () => {}); break;
      case 'revoke':
        void mutate(() => client.revokeArtifactMemberShare(member, button.dataset.shareId, { signal: AbortSignal.timeout(READ_MS) }), 'Link revoked.');
        break;
      case 'disable-sharing':
        if (!globalThis.confirm(DISABLE_CONFIRM)) return;
        void mutate(() => client.setArtifactMemberSharing(selected, false, { signal: AbortSignal.timeout(READ_MS) }), 'Sharing disabled; live links revoked.');
        break;
      case 'enable-sharing':
        void mutate(() => client.setArtifactMemberSharing(selected, true, { signal: AbortSignal.timeout(READ_MS) }), 'Sharing enabled.');
        break;
      case 'purge': $('[data-members-purge]').innerHTML = purgeForm(member, button.dataset.artifactId); $('[data-purge-confirm]').focus(); break;
      case 'purge-cancel': $('[data-members-purge]').innerHTML = ''; break;
      default: break;
    }
  };
  const onInput = event => {
    const input = event.target.closest?.('[data-purge-confirm]');
    if (!input) return;
    const form = input.closest('form');
    form.querySelector('button[type="submit"]').disabled = input.value !== form.dataset.artifactId;
  };
  const onSubmit = event => {
    const form = event.target;
    if (form.matches?.('[data-members-limits]')) {
      event.preventDefault();
      const body = {};
      try {
        for (const [name, , unit] of LIMITS) {
          const input = form.querySelector(`[data-limit="${name}"]`);
          if (input.value.trim() === input.dataset.initial) continue;            // untouched fields are never rewritten
          body[name] = unit === 'MiB' ? mibToBytes(input.value) : countLimit(input.value);
        }
      } catch (error) { status(error.message); return; }
      if (!Object.keys(body).length) { status('No limit changed.'); return; }
      void mutate(() => client.setArtifactMemberLimits(selected, body, { signal: AbortSignal.timeout(READ_MS) }), 'Limits saved.');
    } else if (form.matches?.('[data-members-purge-form]')) {
      event.preventDefault();
      const { member, artifactId } = form.dataset;
      const typed = form.querySelector('[data-purge-confirm]').value;
      if (typed !== artifactId) return;
      void mutate(() => client.purgeArtifactMemberArtifact(member, artifactId, typed, { signal: AbortSignal.timeout(60000) }), 'Artifact purged.');
    }
  };
  const onFilter = () => { void loadLinks().catch(error => { if (alive() && error?.name !== 'AbortError') status(error?.message || 'Links could not load.'); }); };
  container.addEventListener('click', onClick);
  container.addEventListener('input', onInput);
  container.addEventListener('submit', onSubmit);
  $('[data-members-audience]').addEventListener('change', onFilter);
  void load();

  return {
    dispose() {
      disposed = true;
      stop.abort();
      container.removeEventListener('click', onClick);
      container.removeEventListener('input', onInput);
      container.removeEventListener('submit', onSubmit);
      container.replaceChildren();
    },
  };
}
