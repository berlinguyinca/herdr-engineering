// A tiny keyed list updater with the same signature as patchList in packages/patch.js (PR-A), which is not on
// this branch yet. Callers import it from here through ONE import line; when patch.js lands, that line changes to
// `import { patchList } from './patch.js'` and nothing else does.
//
// patchList(container, items, { key, render, update }):
//   - a child is matched to its item by data-key; new keys are rendered and inserted, missing keys are removed;
//   - an existing child is never recreated: update(el, item) changes it in place (focus, open <details> survive);
//   - children are reordered by key only when the order differs, so an unchanged list mutates nothing.
export function patchList(container, items, opts) {
  const { key, render, update } = opts;
  const doc = container.ownerDocument;
  const existing = new Map();
  for (const el of Array.from(container.children)) if (el.dataset.key != null) existing.set(el.dataset.key, el);
  const wanted = items.map((item) => String(key(item)));
  const keep = new Set(wanted);
  for (const [k, el] of existing) if (!keep.has(k)) { el.remove(); existing.delete(k); }
  for (const el of Array.from(container.children)) if (el.dataset.key == null) el.remove();   // placeholders
  items.forEach((item, i) => {
    const k = wanted[i];
    let el = existing.get(k);
    if (el) update?.(el, item);
    else {
      const made = render(item);
      if (typeof made === 'string') { const t = doc.createElement('template'); t.innerHTML = made.trim(); el = t.content.firstElementChild; }
      else el = made;
      el.dataset.key = k;
    }
    if (container.children[i] !== el) container.insertBefore(el, container.children[i] || null);
  });
}
