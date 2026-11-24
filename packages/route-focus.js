// Route change a11y: the document title names the page and keyboard/screen-reader focus lands on its h1.
export const APP_TITLE = 'HerdR Dev Fabric';

// "<page> — HerdR Dev Fabric"; the page is the h1 text, else the route's nav label, else the bare app name.
export function pageTitle(h1Text, fallback = '') {
  const page = [h1Text, fallback].map((v) => String(v ?? '').replace(/\s+/g, ' ').trim()).find(Boolean);
  return page ? `${page} — ${APP_TITLE}` : APP_TITLE;
}

// Sets document.title and moves focus to the page h1 (tabindex="-1": focusable, not a tab stop).
export function announceRoute(root, fallback = '', doc = document) {
  const h1 = root.querySelector('h1');
  doc.title = pageTitle(h1?.textContent, fallback);
  if (!h1) return;
  if (!h1.hasAttribute('tabindex')) h1.setAttribute('tabindex', '-1');
  h1.focus({ preventScroll: true });
}
