// @-mention autocomplete helpers for the room composer. Pure functions, so they unit-test without a DOM.
// A mention is "@" at the start of the text or after whitespace, followed by name characters up to the caret.
const NAME = /^[a-z0-9-]{0,32}$/;

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
