// Terminal button on an agent page. The dashboard is browsed from any fabric machine and the
// agent may run on another host, so a browser cannot open a terminal there and the server never
// spawns one. The server builds the exact command from the verified session record; the viewer
// copies it into a terminal on their own machine. Both forms are always shown, the one matching
// the viewer's machine (when the server could tell) first.
import { escapeHtml as esc } from './herdr-web-components/util.js';

export function attachForms(result) {
  const commands = Array.isArray(result?.commands) ? result.commands.filter((c) => typeof c?.command === 'string' && c.command) : [];
  const first = result?.recommended;
  return [...commands.filter((c) => c.form === first), ...commands.filter((c) => c.form !== first)]
    .map((c) => ({ form: String(c.form || ''), label: String(c.label || ''), command: c.command, recommended: c.form === first }));
}

export function attachError(err) {
  const reason = err?.body?.reason;
  if (reason === 'not_live') return 'This agent is not running or has no live terminal, so there is nothing to attach to.';
  if (reason === 'unsafe_identity') return 'This agent reported a terminal identity that cannot be put in a command safely; no command is offered.';
  if (err?.status === 404) return 'Agent not found.';
  if (err?.status === 401 || err?.status === 403) return 'Only the operator can get a terminal command.';
  return `Could not get the attach command: ${err?.message || 'unknown error'}`;
}

export function attachHtml(result) {
  const forms = attachForms(result);
  if (!forms.length) return '<p class="muted">No attach command available.</p>';
  const viewer = result.viewer_host ? `You are browsing from ${esc(result.viewer_host)}.` : 'Your machine could not be recognised; pick the form that fits.';
  return `<p class="sd-meta">${viewer} Run one of these in a terminal on your machine:</p>`
    + forms.map((f, i) => `<div class="hd-inline" style="margin:6px 0"><span style="min-width:12em">${esc(f.label)}${f.recommended ? ' <strong>(suggested)</strong>' : ''}</span><code style="flex:1;overflow-wrap:anywhere">${esc(f.command)}</code><button type="button" class="hd-btn hd-btn--sm" data-copy="${i}">Copy command</button></div>`).join('')
    + (result.note ? `<p class="sd-meta">${esc(result.note)}</p>` : '');
}

export function mountTerminalAttach(host, client, session, { copyText }) {
  if (!host) return () => {};
  if (!session?.capabilities?.attach_terminal) { host.replaceChildren(); return () => {}; }
  host.innerHTML = '<button type="button" class="hd-btn" data-terminal>Terminal</button><div data-terminal-out></div>';
  const button = host.querySelector('[data-terminal]'), out = host.querySelector('[data-terminal-out]');
  let forms = [], disposed = false;
  const open = async () => {
    button.disabled = true;
    try {
      const result = await client.getTerminalAttach(session.session_id);
      if (disposed) return;
      forms = attachForms(result);
      out.innerHTML = attachHtml(result);
    } catch (err) {
      if (disposed) return;
      out.innerHTML = `<p class="muted">${esc(attachError(err))}</p>`;
    } finally {
      if (!disposed) button.disabled = false;
    }
  };
  const copy = (event) => {
    const btn = event.target.closest('[data-copy]');
    const form = btn && forms[Number(btn.dataset.copy)];
    if (form) copyText(form.command, btn);
  };
  button.addEventListener('click', open);
  out.addEventListener('click', copy);
  return () => { disposed = true; button.removeEventListener('click', open); out.removeEventListener('click', copy); };
}
