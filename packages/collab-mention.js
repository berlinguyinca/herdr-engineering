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
  const m = /([@#/])([^\s@#<>]*)$/.exec(upto);
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
export function findAgent(agents, name) {
  const n = String(name || '').toLowerCase();
  return (agents || []).find((a) => a.address === n) || (agents || []).find((a) => a.name === n)
    || (agents || []).find((a) => (a.aliases || []).includes(n)) || null;
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
  '#'(query, ctx = {}) {
    return rank((ctx.issues || []).map((i) => ({ score: score(query, i.insert.replace(/^#/, ''), i.label), item: { section: 'Issues', icon: 'issue', detail: '', ...i } })));
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
  const plain = String(text).replace(/```[\s\S]*?(?:```|$)/g, ' ').replace(/`[^`\n]*`/g, ' ');
  return [...plain.matchAll(MENTION)].map((m) => m[1].toLowerCase());
}
export function resolvesTo(token, ctx = {}) {
  for (const name of [token, token.replace(/[._-]+$/, '')]) {
    if (!name) continue;
    if (GROUPS.some((g) => g.name === name)) return { kind: 'group', id: name, name };
    const agent = (ctx.agents || []).find((a) => a.address === name);
    if (agent) return { kind: 'agent', id: agent.agent_id, name };
    const person = (ctx.people || []).find((p) => p.handle === name);
    if (person) return { kind: 'person', id: person.user_id, name };
    const alias = findAgent(ctx.agents, name);
    if (alias) return { kind: 'agent', id: alias.agent_id, name: agentKey(alias) };
  }
  return null;
}
export function unresolvedMentions(text, ctx = {}) {
  return [...new Set(mentionTokens(text).filter((t) => !resolvesTo(t, ctx)))];
}
