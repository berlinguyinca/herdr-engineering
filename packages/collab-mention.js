// Completion helpers for the shared composer (room and session workspace). Pure functions, so they
// unit-test without a DOM. Addressing: `@gert` is a person, `@gert.pi` one of gert's agents
// (`<owner>.<harness>[-n]`), `@all`/`@agents`/`@here` groups; old agent names stay valid as aliases.
// The editor completes and validates; it never rewrites what a harness will read.
// A mention is "@" at the start of the text or after whitespace, followed by name characters up to the caret.
const NAME = /^[a-z0-9._-]{0,48}$/;

// The mention being typed at `caret`, or null: { start: index of "@", query: text after it }.
export function mentionAt(text, caret) {
  const upto = String(text).slice(0, caret);
  const at = upto.lastIndexOf('@');
  if (at < 0) return null;
  if (at > 0 && !/\s/.test(upto[at - 1])) return null;       // "me@host" is an address, not a mention
  const query = upto.slice(at + 1);
  return NAME.test(query.toLowerCase()) ? { start: at, query: query.toLowerCase() } : null;
}

// Agents that can take a task and match the query: names that start with it first, then names that contain it.
export function suggest(agents, query, limit = 8) {
  const q = String(query || '').toLowerCase();
  const rank = (a) => (a.name.startsWith(q) ? 0 : (a.name.includes(q) ? 1 : -1));
  return agents
    .filter((a) => (a.actions || []).includes('execute') && rank(a) >= 0)
    .sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name))
    .slice(0, limit);
}

// Replace the typed mention with the chosen agent and a trailing space; returns the new text and caret.
export function applyMention(text, caret, start, name) {
  const head = `${text.slice(0, start)}@${name} `;
  return { text: head + text.slice(caret), caret: head.length };
}

// Debounce: run `fn` once the caller has been quiet for `ms`.
export function quiet(fn, ms = 120, timers = { set: (f, t) => setTimeout(f, t), clear: (id) => clearTimeout(id) }) {
  let t = null;
  const run = (...a) => { timers.clear(t); t = timers.set(() => fn(...a), ms); };
  run.cancel = () => timers.clear(t);
  return run;
}

// ---- sigil completion engine -------------------------------------------------------------------
export const GROUPS = [
  { name: 'all', detail: 'Everyone in this room' },
  { name: 'agents', detail: 'Every agent in this room' },
  { name: 'here', detail: 'People and agents online now' },
];
// Room commands: menu entries only; the collab server interprets them (and only at message start).
export const ROOM_COMMANDS = [
  { name: 'handoff', detail: 'Hand work over: /handoff @from @to note' },
  { name: 'decide', detail: 'Record a decision' },
  { name: 'summarize', detail: 'Ask an online agent to summarize' },
  { name: 'issue', detail: 'Draft a GitHub issue from messages' },
  { name: 'assign', detail: 'Assign an issue: /assign #repo-n @person' },
];
const BEFORE = /(^|[\s([{])$/;                     // a sigil opens only at the start or after whitespace/bracket
const AGENT_BEFORE = /(?:^|[\s([{])@([a-z0-9._-]{1,48})\s+$/i;

// The completion being typed at `caret`: { sigil, query, start } (plus `agent` for "@agent /skill"), or null.
// `!` (a harness shell escape) and `$` are never completed.
export function completionAt(text, caret) {
  const upto = String(text).slice(0, caret);
  // an @ query may hold '#' and '/' (`@org/repo#12`, `@src/app.js`); # and / queries stop at the next sigil
  const m = /(@)([^\s@<>]*)$/.exec(upto) || /([#/])([^\s@#<>]*)$/.exec(upto);
  if (!m) return null;
  const start = upto.length - m[0].length, sigil = m[1], query = m[2].toLowerCase(), head = upto.slice(0, start);
  if (!BEFORE.test(head)) return null;                                          // a@b.com, C:\x\@y, and/or
  if (sigil === '#' && !/^[a-z0-9._/-]*$/.test(query)) return null;
  if (sigil !== '/') return { sigil, query, start };
  if (/[/\\]/.test(query)) return null;                                         // a path, not a command
  if (!head.trim()) return { sigil, query, start };                             // room command: message start only
  const agent = AGENT_BEFORE.exec(head);
  return agent && !agent.index && !head.slice(0, agent.index).trim()
    ? { sigil, query, start, agent: agent[1].toLowerCase() } : null;
}

const SECTIONS = ['People', 'Agents', 'Files', 'Issues', 'Commands', 'Skills'];
// 0 exact, 1 prefix, 2 prefix of a part (after . / - : _), 3 contains, -1 no match.
function score(query, ...names) {
  let best = -1;
  for (const raw of names) {
    const name = String(raw || '').toLowerCase();
    if (!name) continue;
    const s = name === query ? 0 : name.startsWith(query) ? 1
      : name.split(/[./:_-]/).some((part) => part.startsWith(query)) ? 2 : name.includes(query) ? 3 : -1;
    if (s >= 0 && (best < 0 || s < best)) best = s;
  }
  return best;
}
function rank(rows, limit = 12) {
  return rows.filter((r) => r.score >= 0)
    .sort((a, b) => a.score - b.score || SECTIONS.indexOf(a.item.section) - SECTIONS.indexOf(b.item.section)
      || a.item.insert.localeCompare(b.item.insert))
    .slice(0, limit).map((r) => r.item);
}
const agentKey = (a) => a.address || a.name;
// The one agent a name addresses (address, current name or old name), or null when it addresses a person,
// nobody, or more than one principal (the server's rule: an ambiguous name addresses nobody).
export function findAgent(agents, name) {
  const found = lookup(String(name || '').toLowerCase(), { agents });
  return found?.hit?.kind === 'agent' ? (agents || []).find((a) => a.agent_id === found.hit.id) || null : null;
}

export const providers = {
  '@'(query, ctx = {}) {
    const rows = [];
    for (const g of GROUPS) rows.push({ score: g.name.startsWith(query) ? 1 : -1, item: { insert: `@${g.name}`, label: `@${g.name}`, section: 'People', icon: 'group', detail: g.detail } });
    for (const p of ctx.people || []) {
      if (!p.handle) continue;
      rows.push({ score: score(query, p.handle, p.label), item: { insert: `@${p.handle}`, label: `@${p.handle}`, section: 'People', icon: 'person', detail: [p.label, p.online ? 'online' : ''].filter(Boolean).join(' · ') } });
    }
    for (const a of ctx.agents || []) {
      const key = agentKey(a);
      if (!key) continue;
      const also = [a.name, ...(a.aliases || [])].filter((n) => n && n !== key);
      rows.push({ score: score(query, key, a.label, ...also), item: { insert: `@${key}`, label: `@${key}`, section: 'Agents', icon: 'robot',
        detail: [a.label, a.harness_type, also.length ? `also @${also.join(', @')}` : ''].filter(Boolean).join(' · ') } });
    }
    for (const f of ctx.files || []) {
      rows.push({ score: score(query, f.path, f.label), item: { insert: f.insert || `@${f.path}`, label: f.label || f.path, section: 'Files', icon: 'file', detail: f.detail || 'File' } });
    }
    return rank(rows);
  },
  // Issues come from the server's GitHub cache (in-scope owners only); `insert` is the shortest unambiguous
  // form (`#repo-n`, or `owner/repo#n` when two owners share the name). No cache: one row that says so and
  // inserts nothing, so Enter still sends.
  '#'(query, ctx = {}) {
    if (ctx.issuesReady === false) {
      return [{ insert: '', label: 'GitHub cache not ready', section: 'Issues', icon: 'issue', notice: true,
        detail: 'The message still sends; #references resolve once the cache is filled' }];
    }
    return rank((ctx.issues || []).map((i) => {
      const forms = i.repo ? [`${i.repo}-${i.number}`, `${i.owner}/${i.repo}#${i.number}`] : [];
      const detail = i.detail ?? [i.title, i.state].filter(Boolean).join(' · ');
      return { score: score(query, i.insert.replace(/^#/, ''), ...forms, i.label), item: { section: 'Issues', icon: 'issue', label: i.insert, ...i, detail } };
    }));
  },
  '/'(query, ctx = {}) {
    if (ctx.agent) {
      const agent = findAgent(ctx.agents, ctx.agent);
      return rank((agent?.skills || []).map((s) => ({ score: score(query, s), item: { insert: `/${s}`, label: `/${s}`, section: 'Skills', icon: 'skill', detail: `${agentKey(agent)} skill` } })));
    }
    return (ctx.commands || []).filter((c) => !query || c.name.startsWith(query))
      .map((c) => ({ insert: `/${c.name}`, label: `/${c.name}`, section: 'Commands', icon: 'command', detail: c.detail || '' }));
  },
};

// Replace the typed fragment [start, caret) with the item's literal text and a trailing space.
export function applyCompletion(text, caret, start, item) {
  const head = `${String(text).slice(0, start)}${item.insert} `;
  return { text: head + String(text).slice(caret), caret: head.length };
}

// Names after "@" that would address nobody, using the server's grammar: outside code, not inside
// a word, trailing punctuation ignored. Each name once, in order.
const MENTION = /(?<![^\s([{])@([A-Za-z0-9._-]{1,48})(?![A-Za-z0-9._-])/g;
export function mentionTokens(text) {
  const plain = String(text).replace(/(```|~~~)[\s\S]*?(?:\1|$)/g, ' ').replace(/`[^`\n]*`/g, ' ');
  return [...plain.matchAll(MENTION)].map((m) => m[1].toLowerCase());
}
// Precedence as on the server: groups; agent addresses; person handles together with agents' current
// names (a collision between the two addresses nobody); then old names. Two principals at one level -> ambiguous.
function lookup(name, ctx) {
  if (GROUPS.some((g) => g.name === name)) return { hit: { kind: 'group', id: name, name } };
  const agents = ctx.agents || [], people = ctx.people || [];
  const agentHit = (a) => ({ kind: 'agent', id: a.agent_id, name: agentKey(a), agent: a });
  const levels = [
    agents.filter((a) => a.address === name).map(agentHit),
    [...people.filter((p) => p.handle === name).map((p) => ({ kind: 'person', id: p.user_id, name: p.handle })),
      ...agents.filter((a) => a.name === name).map(agentHit)],
    agents.filter((a) => (a.aliases || []).includes(name)).map(agentHit),
  ];
  for (const level of levels) {
    const unique = [...new Map(level.map((h) => [`${h.kind}:${h.id}`, h])).values()];
    if (unique.length === 1) { const { agent, ...hit } = unique[0]; return { hit }; }
    if (unique.length > 1) {
      return { ambiguous: unique.filter((h) => h.kind === 'agent' && h.agent.address).map((h) => `@${h.agent.address}`).sort() };
    }
  }
  return null;
}
// The first candidate (token, then token without trailing punctuation) that means anything decides:
// { hit } | { ambiguous: ['@unambiguous.form', ...] } | null.
export function resolveToken(token, ctx = {}) {
  for (const name of [token, token.replace(/[._-]+$/, '')]) {
    const found = name && lookup(name, ctx);
    if (found) return found;
  }
  return null;
}
export function resolvesTo(token, ctx = {}) {
  return resolveToken(token, ctx)?.hit || null;
}
export function ambiguousMentions(text, ctx = {}) {
  const out = new Map();
  for (const t of mentionTokens(text)) {
    const found = resolveToken(t, ctx);
    const name = t.replace(/[._-]+$/, '');
    if (found?.ambiguous && !out.has(name)) out.set(name, { name, forms: found.ambiguous });
  }
  return [...out.values()];
}
export function unresolvedMentions(text, ctx = {}) {
  return [...new Set(mentionTokens(text).filter((t) => !resolveToken(t, ctx)))];
}

// Generic items (`{insert, label, section, icon, detail}`) filtered and ranked by the typed query.
export function filterItems(items, query) {
  return rank((items || []).map((item) => ({ score: score(query, item.insert.replace(/^[@#/]/, ''), item.label), item })));
}

// Mentions in rendered message HTML as chips. Only names the server resolved for this message (its
// `mentions`) become chips; code, links and unknown names stay text. The chip keeps the literal text.
const CHIP_ICON = { person: ['person', '👤'], agent: ['robot', '🤖'], group: ['group', '👥'] };
const escText = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export function mentionChips(html, mentions, ctx = {}) {
  const list = mentions || [];
  if (!list.length) return html;
  const hit = (token) => {
    for (const name of [token, token.replace(/[._-]+$/, '')]) {
      if (!name) continue;
      const byName = list.find((m) => m.name === name);
      if (byName) return { m: byName, len: name.length };
      const r = lookup(name, ctx)?.hit;
      const byId = r && list.find((m) => m.kind === r.kind && m.id === r.id);
      if (byId) return { m: byId, len: name.length };
    }
    return null;
  };
  let skip = 0;
  return String(html).split(/(<[^>]*>)/).map((part) => {
    if (part.startsWith('<')) {
      const tag = /^<(\/?)(code|pre|a)\b/i.exec(part);
      if (tag) skip = Math.max(0, skip + (tag[1] ? -1 : 1));
      return part;
    }
    if (skip) return part;
    return part.replace(/(^|[\s([{])@([A-Za-z0-9._-]{1,48})(?![A-Za-z0-9._-])/g, (all, pre, token) => {
      const found = hit(token.toLowerCase());
      if (!found) return all;
      const [icon, glyph] = CHIP_ICON[found.m.kind] || CHIP_ICON.person;
      const label = `${found.m.kind === 'agent' ? 'Agent' : found.m.kind === 'group' ? 'Group' : 'Person'} @${found.m.name}`;
      return `${pre}<span class="cx-mention cx-mention--${escText(found.m.kind)}" data-mention-kind="${escText(found.m.kind)}" data-mention-id="${escText(found.m.id)}" title="${escText(label)}"><span class="cx-mention__icon" data-icon="${icon}" aria-hidden="true">${glyph}</span>@${token.slice(0, found.len)}</span>${token.slice(found.len)}`;
    });
  }).join('');
}

// `#` references in rendered message HTML as chips. Only references the server resolved for this message
// (its `refs`) become chips; code, links and other text stay as they are. The chip keeps the literal text and
// shows the title and state the server read from the GitHub cache for this reader (none: coordinates only).
const REF = /(^|[\s([{])(#([A-Za-z0-9_.-]{1,100}?)-([0-9]{1,10})|([A-Za-z0-9][A-Za-z0-9-]{0,38})\/([A-Za-z0-9_.-]{1,100})#([0-9]{1,10}))(?![A-Za-z0-9_#/-]|\.[A-Za-z0-9_])/g;
export function refChips(html, refs) {
  const list = refs || [];
  if (!list.length) return html;
  const same = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();
  let skip = 0;
  return String(html).split(/(<[^>]*>)/).map((part) => {
    if (part.startsWith('<')) {
      const tag = /^<(\/?)(code|pre|a)\b/i.exec(part);
      if (tag) skip = Math.max(0, skip + (tag[1] ? -1 : 1));
      return part;
    }
    if (skip) return part;
    return part.replace(REF, (all, pre, text, shortRepo, shortNo, owner, repo, no) => {
      const ref = shortRepo
        // a short token is a chip only when the server resolved that short form: an ambiguous `#app-3` stays
        // text even when `other/app#3` in the same message resolved
        ? list.find((r) => (r.forms || []).includes('short') && same(r.repo, shortRepo) && Number(r.number) === Number(shortNo))
        : list.find((r) => (r.forms || ['long']).includes('long') && same(r.owner, owner) && same(r.repo, repo) && Number(r.number) === Number(no));
      if (!ref) return all;
      const where = `${ref.owner}/${ref.repo}#${Number(ref.number)}`;
      const state = ref.state === 'open' || ref.state === 'closed' || ref.state === 'merged' ? ref.state : '';
      const label = [`${ref.kind === 'pr' ? 'Pull request' : 'Issue'} ${where}`, ref.title, state].filter(Boolean).join(' · ');
      return `${pre}<span class="cx-mention cx-ref${state ? ` cx-ref--${state}` : ''}" data-ref="${escText(where)}"${state ? ` data-ref-state="${state}"` : ''} title="${escText(label)}"><span class="cx-mention__icon" data-icon="issue" aria-hidden="true">${ref.kind === 'pr' ? '⑂' : '◎'}</span>${text}${ref.title ? `<span class="cx-ref__title">${escText(ref.title)}</span>` : ''}${state ? `<span class="cx-ref__state">${state}</span>` : ''}</span>`;
    });
  }).join('');
}
