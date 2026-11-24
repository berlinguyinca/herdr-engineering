// The share preview's "Rich view" toggle. The server inlines this file into a report share's preview page ONLY when
// the approver opted in to sharing the author's HTML page. It builds the one frame a share page may hold: an iframe with
// an empty `sandbox` (no scripts, no same-origin, no forms, no popups) whose src is the bundled author page
// (`author-page.html`, a sibling of the preview), which the share host also serves under a `sandbox` CSP.
// tests/control_plane/test_no_frames.py allows this file and nothing else, and pins this shape.
(function () {
  var SRC = 'author-page.html';
  var root = document.querySelector('[data-rich-view]');
  if (!root) return;
  var toggle = root.querySelector('[data-rich-toggle]');
  var slot = root.querySelector('[data-rich-slot]');
  root.hidden = false;
  toggle.addEventListener('click', function () {
    var show = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(show));
    toggle.textContent = show ? 'Hide rich view' : 'Show rich view';
    if (!show) { slot.replaceChildren(); return; }
    var frame = document.createElement('iframe');
    frame.setAttribute('sandbox', '');
    frame.setAttribute('src', SRC);
    frame.setAttribute('title', 'Report rich view');
    frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.setAttribute('data-rich-frame', '');
    var note = document.createElement('p');
    note.className = 'sub';
    note.setAttribute('data-rich-note', '');
    note.textContent = 'Scripts are disabled in this view';
    slot.replaceChildren(frame, note);
  });
})();
