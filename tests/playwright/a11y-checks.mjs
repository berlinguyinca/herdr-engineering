// In-house WCAG 2.2 AA spot checks for the HerdR collaboration page (T16 UI gates).
//
// This repo takes no new dependencies (DESIGN.md: "VanillaJS... no new dependencies"; package.json carries only
// playwright), so axe-core is NOT used. These checks are a deliberately small, deterministic subset and are NOT a
// conformance claim: automated checks decide only a minority of the AA criteria (see autospec-design
// gate.review-not-auto-merged). Each check returns a list of findings {rule, wcag, node, detail}; the returned array also
// carries `.stats` ({controls, textNodes, pixelChecked, focused}) so a test can assert that it really looked at something.
//
// Everything runs inside the page, against computed styles and the live DOM. Evaluation goes through a string expression so the
// page's own Content-Security-Policy is irrelevant (it is a devtools evaluation, not page script).

/** Colour helpers shared by every in-page check. Serialised with toString(): it must not reference anything outside itself. */
function colorLib() {
  const parse = (c) => {
    let m = /^rgba?\(([^)]+)\)$/.exec(c);
    if (m) { const p = m[1].split(/[ ,\/]+/).filter(Boolean).map((x) => (x.endsWith('%') ? parseFloat(x) * 2.55 : Number(x))); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; }
    m = /^color\(srgb ([^)]+)\)$/.exec(c);
    if (m) { const p = m[1].split(/[ \/]+/).filter(Boolean).map(Number); return [p[0] * 255, p[1] * 255, p[2] * 255, p.length > 3 ? p[3] : 1]; }
    return null;
  };
  const over = (top, bottom) => { const a = top[3]; return [top[0] * a + bottom[0] * (1 - a), top[1] * a + bottom[1] * (1 - a), top[2] * a + bottom[2] * (1 - a), 1]; };
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const ratio = (a, b) => { const x = lum(a); const y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  // The colour behind an element: stacked background colours up the tree. `unknown` when an image or gradient is involved.
  const bgOf = (el) => {
    const stack = [];
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.backgroundImage !== 'none') return { unknown: true };
      const c = parse(cs.backgroundColor);
      if (c && c[3] > 0) { stack.push(c); if (c[3] >= 1) break; }
    }
    let base = parse(getComputedStyle(document.body).backgroundColor);
    if (!base || base[3] < 1) base = [255, 255, 255, 1];
    let acc = stack.length && stack[stack.length - 1][3] >= 1 ? stack.pop() : base;
    while (stack.length) acc = over(stack.pop(), acc);
    return { color: acc };
  };
  // Opacity of the element AND every ancestor (a faint parent fades its text, and the text's own colour says nothing about it).
  const opacityOf = (el) => { let o = 1; for (let e = el; e && e.nodeType === 1; e = e.parentElement) o *= Number(getComputedStyle(e).opacity); return Number.isFinite(o) ? o : 1; };
  const sel = (el) => {
    if (!(el instanceof Element)) return String(el);
    const id = el.id ? `#${el.id}` : '';
    const cls = el.classList.length ? '.' + [...el.classList].slice(0, 2).join('.') : '';
    const data = [...el.attributes].filter((a) => a.name.startsWith('data-')).slice(0, 1).map((a) => `[${a.name}="${String(a.value).slice(0, 24)}"]`).join('');
    return `${el.tagName.toLowerCase()}${id}${cls}${data}`;
  };
  const visible = (el) => {
    for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.display === 'none' || cs.visibility === 'hidden' || e.hidden || e.getAttribute('aria-hidden') === 'true') return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  return { parse, over, lum, ratio, bgOf, opacityOf, sel, visible };
}

const inPage = (page, fn, arg) => page.evaluate(`((arg) => { const L = (${colorLib.toString()})(); return (${fn.toString()})(arg, L); })(${JSON.stringify(arg)})`);

const ROLES = new Set(['alert', 'alertdialog', 'application', 'article', 'banner', 'blockquote', 'button', 'caption', 'cell', 'checkbox', 'code', 'columnheader', 'combobox', 'complementary', 'contentinfo', 'definition', 'deletion', 'dialog', 'directory', 'document', 'emphasis', 'feed', 'figure', 'form', 'generic', 'grid', 'gridcell', 'group', 'heading', 'img', 'insertion', 'link', 'list', 'listbox', 'listitem', 'log', 'main', 'marquee', 'math', 'menu', 'menubar', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'meter', 'navigation', 'none', 'note', 'option', 'paragraph', 'presentation', 'progressbar', 'radio', 'radiogroup', 'region', 'row', 'rowgroup', 'rowheader', 'scrollbar', 'search', 'searchbox', 'separator', 'slider', 'spinbutton', 'status', 'strong', 'subscript', 'superscript', 'switch', 'tab', 'table', 'tablist', 'tabpanel', 'term', 'textbox', 'time', 'timer', 'toolbar', 'tooltip', 'tree', 'treegrid', 'treeitem']);

function staticInPage(arg, L) {
    const { parse, over, ratio, bgOf, opacityOf, sel, visible } = L;
    const scopeSel = arg.scope;
    const out = [];
    const stats = { controls: 0, textNodes: 0 };
    const pixelTargets = [];
    const scope = document.querySelector(scopeSel) || document.body;
    const add = (rule, wcag, el, detail) => out.push({ rule, wcag, node: sel(el), detail });
    const all = [...scope.querySelectorAll('*')].filter((e) => !['SCRIPT', 'STYLE', 'LINK', 'META', 'TITLE', 'HEAD'].includes(e.tagName));
    const ROLE_LIST = arg.roles;

    // ---- accessible name (a pragmatic subset of accname 1.2)
    const text = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim();
    const nameOf = (el) => {
      const lb = el.getAttribute('aria-labelledby');
      if (lb) { const t = lb.split(/\s+/).map((i) => document.getElementById(i)).filter(Boolean).map(text).join(' ').trim(); if (t) return t; }
      const al = (el.getAttribute('aria-label') || '').trim();
      if (al) return al;
      if (el.labels && el.labels.length) { const t = [...el.labels].map(text).join(' ').trim(); if (t) return t; }
      if (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type) && el.value) return el.value;
      if (el.tagName === 'IMG') return (el.getAttribute('alt') || '').trim();
      if (el.tagName === 'SVG' || el.tagName === 'svg') { const t = el.querySelector(':scope > title'); if (t) return text(t); }
      if (el.tagName === 'FIELDSET') { const lg = el.querySelector(':scope > legend'); if (lg) return text(lg); }
      const t = text(el);
      if (t) return t;
      return (el.getAttribute('title') || '').trim();
    };
    const NATIVE = 'button,select,textarea,summary,a[href],input:not([type=hidden])';
    const INTERACTIVE_ROLES = ['button', 'tab', 'link', 'checkbox', 'radio', 'switch', 'menuitem', 'option', 'textbox', 'combobox', 'slider', 'treeitem'];
    const isInteractive = (el) => {
      const role = el.getAttribute('role');
      if (el.matches(NATIVE)) return true;
      if (role && INTERACTIVE_ROLES.includes(role)) return true;
      return el.hasAttribute('tabindex') && el.tabIndex >= 0 && el.tagName !== 'BODY';
    };
    const focusableNow = (el) => (el.matches(NATIVE) ? !el.disabled : el.hasAttribute('tabindex')) && el.tabIndex >= 0;
    const interactive = all.filter((el) => visible(el) && isInteractive(el));
    stats.controls = interactive.length;
    for (const el of interactive) {
      if (!nameOf(el)) add('name-role-value', '4.1.2', el, 'interactive element has no accessible name');
    }
    for (const el of all) {
      if (el.tagName === 'IMG' && !el.hasAttribute('alt') && el.getAttribute('role') !== 'presentation') add('image-alt', '1.1.1', el, 'img without alt');
      if (el.getAttribute('role') === 'img' && !nameOf(el)) add('image-alt', '1.1.1', el, 'role=img without a name');
    }
    // ---- structure that assistive technology misreads
    for (const el of all) {
      const role = el.getAttribute('role');
      if (role) {
        const tokens = role.trim().split(/\s+/);
        if (!tokens.some((t) => ROLE_LIST.includes(t))) add('invalid-role', '4.1.2', el, `role="${role}" is not a WAI-ARIA role`);
      }
      const ti = el.getAttribute('tabindex');
      if (ti !== null && Number(ti) > 0) add('positive-tabindex', '2.4.3', el, `tabindex="${ti}" overrides the natural focus order`);
      if (el.matches('[aria-hidden="true"]') || el.closest('[aria-hidden="true"]')) {
        const bad = (el.matches('[aria-hidden="true"]') ? [el, ...el.querySelectorAll('*')] : [el]).filter((e) => e === el && focusableNow(e) && !e.closest('[hidden],[inert]') && getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden');
        for (const b of bad) add('aria-hidden-focusable', '4.1.2', b, 'focusable element is inside aria-hidden="true" (hidden from assistive technology but reachable by Tab)');
      }
      // a WIDGET containing a widget (a scrollable region or tab panel that is merely focusable is not a widget)
      if (visible(el) && (el.matches(NATIVE) || (role && INTERACTIVE_ROLES.includes(role))) && !el.matches('input[type=checkbox],input[type=radio]')) {
        const inner = [...el.querySelectorAll('*')].filter((c) => isInteractive(c) && c.tagName !== 'LABEL');
        if (inner.length && !el.matches('[role=tablist],[role=listbox],[role=grid],[role=menu],[role=toolbar],[role=group]')) add('nested-interactive', '4.1.2', el, `interactive element contains ${inner.length} interactive descendant(s): ${sel(inner[0])}`);
      }
      if (role === 'img' || role === 'image') {
        const inner = [...el.querySelectorAll('*')].filter((c) => isInteractive(c));
        if (inner.length) add('img-wraps-interactive', '4.1.2', el, `role=img makes its ${inner.length} interactive child(ren) invisible to assistive technology: ${sel(inner[0])}`);
      }
      // live regions: the politeness must match the role, and anything that announces a state should say so
      const live = el.getAttribute('aria-live');
      if (live !== null && !['off', 'polite', 'assertive'].includes(live)) add('live-region', '4.1.3', el, `aria-live="${live}" is not a valid value`);
      if (role === 'status' && (live === 'assertive' || live === 'off')) add('live-region', '4.1.3', el, `role=status with aria-live="${live}" contradicts itself`);
      if (role === 'alert' && (live === 'polite' || live === 'off')) add('live-region', '4.1.3', el, `role=alert with aria-live="${live}" contradicts itself`);
      if (!role && live === null && visible(el) && el.children.length === 0 && /(^|[\s_-])(toast|snackbar|flash|notice|status-msg|alert|error-msg|form-error)([\s_-]|$)/.test(`${el.id} ${el.className && el.className.baseVal === undefined ? el.className : ''}`) && !el.closest('[role=status],[role=alert],[aria-live]')) add('live-region', '4.1.3', el, 'looks like a status or error message but is not a live region');
    }
    for (const el of scope.querySelectorAll('[aria-live=assertive]')) if (!el.matches('[role=alert]')) add('live-assertive', '4.1.3', el, 'assertive live region (interrupts); prefer polite/role=status');
    // ---- ARIA validity
    const ids = new Map();
    for (const el of scope.querySelectorAll('[id]')) ids.set(el.id, (ids.get(el.id) || 0) + 1);
    for (const [id, n] of ids) if (n > 1) add('duplicate-id', '4.1.1', document.getElementById(id), `id "${id}" used ${n} times`);
    for (const el of all) {
      for (const attr of ['aria-controls', 'aria-labelledby', 'aria-describedby', 'aria-owns']) {
        const v = el.getAttribute(attr);
        if (v) for (const id of v.split(/\s+/)) if (!document.getElementById(id)) add('aria-ref', '4.1.2', el, `${attr} points at missing id "${id}"`);
      }
      const role = el.getAttribute('role');
      if (role === 'tab') {
        if (!el.closest('[role=tablist]')) add('tab-needs-tablist', '1.3.1', el, 'role=tab outside a role=tablist');
        if (!el.hasAttribute('aria-selected')) add('tab-selected', '4.1.2', el, 'role=tab without aria-selected');
      }
      if (role === 'tablist' && visible(el)) {
        const tabs = [...el.querySelectorAll('[role=tab]')];
        if (!tabs.length) add('tablist-empty', '1.3.1', el, 'role=tablist with no tabs');
      }
    }
    // ---- headings: no skipped levels
    const hs = [...scope.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(visible).map((h) => Number(h.tagName[1]));
    for (let i = 1; i < hs.length; i += 1) if (hs[i] - hs[i - 1] > 1) { add('heading-order', '1.3.1', scope.querySelectorAll('h1,h2,h3,h4,h5,h6')[i], `h${hs[i - 1]} followed by h${hs[i]}`); break; }
    // ---- language / title / zoom
    if (scope === document.body && !document.querySelector('main, [role=main]')) add('landmark-main', '2.4.1', document.body, 'the page has no <main> landmark to skip to');
    if (!document.documentElement.getAttribute('lang')) add('html-lang', '3.1.1', document.documentElement, 'no lang on <html>');
    if (!document.title.trim()) add('document-title', '2.4.2', document.documentElement, 'empty <title>');
    const vp = document.querySelector('meta[name=viewport]');
    if (vp && /user-scalable\s*=\s*(no|0)|maximum-scale\s*=\s*1(\.0)?\b/i.test(vp.content)) add('zoom-not-blocked', '1.4.4', vp, 'viewport blocks zoom');
    if (!vp) add('viewport-meta', '1.4.10', document.documentElement, 'no viewport meta');
    for (const el of scope.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea')) {
      if (visible(el) && !nameOf(el)) add('label', '3.3.2', el, 'form control without a label');
    }

    // ---- contrast of rendered text (1.4.3). Text on an image or gradient is measured on the rendered pixels afterwards.
    const contrastFails = new Map();
    const judge = (el, fgColor, bgColor, px, weight, what) => {
      const solid = over(fgColor, bgColor);
      const r = ratio(solid, bgColor);
      const large = px >= 24 || (px >= 18.66 && Number(weight) >= 700);
      const need = large ? 3 : 4.5;
      if (r + 0.005 < need) {
        const key = `${what}|${solid.map(Math.round)}|${bgColor.map(Math.round)}|${large}`;
        const hit = contrastFails.get(key);
        if (hit) hit.n += 1;
        else contrastFails.set(key, { el, r, need, n: 1, what, fg: solid.map(Math.round).join(','), bg: bgColor.map(Math.round).join(',') });
      }
    };
    const seen = new Set();
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    let tn;
    while ((tn = walker.nextNode())) {
      if (!tn.nodeValue.trim()) continue;
      const el = tn.parentElement;
      if (!el || seen.has(el) || !visible(el) || el.closest('script,style')) continue;
      if (el.closest('[disabled],[aria-disabled=true]')) continue;       // inactive components are exempt (1.4.3)
      seen.add(el); stats.textNodes += 1;
      const cs = getComputedStyle(el);
      const svg = el.closest('svg');
      const paint = svg && el.namespaceURI === 'http://www.w3.org/2000/svg' ? cs.fill : cs.color;
      const fg = parse(paint);
      if (!fg) continue;                       // fill: none / a paint server (gradient): not judged
      const alpha = fg[3] * opacityOf(el);
      let bg;
      if (svg && el.namespaceURI === 'http://www.w3.org/2000/svg') {
        // the shape drawn under the text (a node box, a bar) decides the background, else whatever is behind the svg
        const r = el.getBoundingClientRect();
        const under = document.elementsFromPoint(Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1), Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1))
          .find((e) => e !== el && e.ownerSVGElement && e.namespaceURI === 'http://www.w3.org/2000/svg' && /^(rect|path|circle|ellipse|polygon)$/.test(e.localName) && parse(getComputedStyle(e).fill) && parse(getComputedStyle(e).fill)[3] > 0 && getComputedStyle(e).display !== 'none');
        const b = bgOf(svg.parentElement || svg);
        if (b.unknown) { continue; }
        if (under) { const f = parse(getComputedStyle(under).fill); f[3] *= Number(getComputedStyle(under).fillOpacity) * opacityOf(under); bg = { color: over(f, b.color) }; } else bg = b;
      } else bg = bgOf(el);
      if (bg.unknown) {
        if (pixelTargets.length < 40) { const n = pixelTargets.length; el.setAttribute('data-a11y-px', String(n)); pixelTargets.push({ n, fg: [fg[0], fg[1], fg[2], alpha], px: parseFloat(cs.fontSize), weight: cs.fontWeight, node: sel(el) }); }
        continue;
      }
      judge(el, [fg[0], fg[1], fg[2], alpha], bg.color, parseFloat(cs.fontSize), cs.fontWeight, 'text');
    }
    // pseudo-element text (::before / ::after generated content) and placeholders
    for (const el of all) {
      if (!visible(el) || el.closest('[disabled],[aria-disabled=true]')) continue;
      for (const pseudo of ['::before', '::after']) {
        const ps = getComputedStyle(el, pseudo);
        const content = ps.content;
        if (!content || content === 'none' || content === 'normal' || /^(""|'')$/.test(content) || ps.display === 'none' || ps.visibility === 'hidden') continue;
        const fg = parse(ps.color); if (!fg) continue;
        let bg = bgOf(el);
        const own = parse(ps.backgroundColor);
        if (bg.unknown) continue;
        if (own && own[3] > 0) bg = { color: over(own, bg.color) };
        stats.textNodes += 1;
        judge(el, [fg[0], fg[1], fg[2], fg[3] * opacityOf(el)], bg.color, parseFloat(ps.fontSize), ps.fontWeight, `${pseudo} text`);
      }
      if (el.matches('input[placeholder],textarea[placeholder]') && !el.value) {
        const ps = getComputedStyle(el, '::placeholder');
        const fg = parse(ps.color); if (!fg) continue;
        const own = parse(getComputedStyle(el).backgroundColor);
        let bg = bgOf(el.parentElement || el);
        if (bg.unknown) continue;
        if (own && own[3] > 0) bg = { color: over(own, bg.color) };
        stats.textNodes += 1;
        judge(el, [fg[0], fg[1], fg[2], fg[3] * Number(ps.opacity || 1) * opacityOf(el)], bg.color, parseFloat(getComputedStyle(el).fontSize), 400, '::placeholder');
      }
    }
    for (const f of contrastFails.values()) add('color-contrast', '1.4.3', f.el, `${f.r.toFixed(2)}:1 < ${f.need}:1 (${f.what} rgb(${f.fg}) on rgb(${f.bg})), ${f.n} occurrence(s)`);

    // ---- target size (2.5.8, 24 CSS px, with the spacing exception)
    for (const el of interactive) {
      const t = el.tagName.toLowerCase();
      if (t === 'input' && ['checkbox', 'radio'].includes(el.type)) continue;      // native control; its label is the target
      if (t === 'a' && el.closest('p,li,span,small,label') && getComputedStyle(el).display === 'inline') continue;   // inline in text (exception)
      const r = el.getBoundingClientRect();
      if (r.width + 0.5 < 24 || r.height + 0.5 < 24) {
        const cx = r.left + r.width / 2; const cy = r.top + r.height / 2;
        const clash = interactive.some((o) => {
          if (o === el || o.contains(el) || el.contains(o)) return false;
          const q = o.getBoundingClientRect();
          const dx = Math.max(q.left - cx, 0, cx - q.right); const dy = Math.max(q.top - cy, 0, cy - q.bottom);
          return Math.hypot(dx, dy) < 12;
        });
        if (clash) add('target-size', '2.5.8', el, `${Math.round(r.width)}x${Math.round(r.height)}px with another target within 24px`);
      }
    }
    // ---- hover-only affordances: :hover rules that reveal something must have a :focus equivalent (static scan)
    const hoverOnly = [];
    for (const sheet of document.styleSheets) {
      let rules; try { rules = sheet.cssRules; } catch { continue; }
      const walk = (list) => {
        for (const r of list) {
          if (r.cssRules && !r.selectorText) { walk(r.cssRules); continue; }
          const s = r.selectorText || '';
          if (!s.includes(':hover')) continue;
          const reveals = /(^|;)\s*(display|visibility|opacity)\s*:/.test(r.style.cssText);
          if (!reveals) continue;
          const sibling = s.replace(/:hover/g, ':focus-visible');
          const sibling2 = s.replace(/:hover/g, ':focus-within');
          let ok = false;
          const scan = (l) => { for (const q of l) { if (q.cssRules && !q.selectorText) scan(q.cssRules); else if (q.selectorText && (q.selectorText.includes(sibling) || q.selectorText.includes(sibling2) || q.selectorText.includes(':focus'))) ok = true; } };
          scan(rules);
          if (!ok) hoverOnly.push(s);
        }
      };
      walk(rules);
    }
    for (const s of hoverOnly) add('hover-has-equivalent', '2.1.1', document.documentElement, `hover reveals content with no focus equivalent: ${s}`);
    // ---- clickable things that cannot be focused (cursor:pointer, not inside a focusable control)
    for (const el of all) {
      if (!visible(el)) continue;
      const cs = getComputedStyle(el);
      if (cs.cursor !== 'pointer') continue;
      if (el.matches('button,a[href],input,select,textarea,summary,label,[tabindex],[role=button],[role=tab],option') || el.closest('button,a[href],summary,label,[tabindex],[role=button]')) continue;
      if (el.closest('svg') && el.ownerSVGElement && el.closest('[tabindex],[role=button]')) continue;
      add('keyboard-operable', '2.1.1', el, 'looks clickable (cursor:pointer) but is not focusable');
    }
    // ---- `transition: all` animates properties nobody chose (layout, colour, shadow together) and defeats reduced-motion overrides
    for (const el of all) {
      if (!visible(el)) continue;
      const cs = getComputedStyle(el);
      const durs = cs.transitionDuration.split(',').map((d) => (d.trim().endsWith('ms') ? parseFloat(d) : parseFloat(d) * 1000));
      const props = cs.transitionProperty.split(',').map((p) => p.trim());
      if (props.some((p, i) => p === 'all' && (durs[i % durs.length] || 0) > 1)) add('transition-all', '2.3.3', el, `transition: all ${cs.transitionDuration} (name the properties that should animate)`);
    }
    return { out, stats, pixelTargets };
}

/** Contrast of text drawn over an image or gradient, measured on the RENDERED pixels: the text is made transparent, the area
 *  under it is photographed, and the worst (1st percentile) background pixel decides the ratio. */
async function pixelContrast(page, targets) {
  const out = [];
  for (const t of targets) {
    const sel = `[data-a11y-px="${t.n}"]`;
    const loc = page.locator(sel).first();
    await loc.scrollIntoViewIfNeeded().catch(() => {});
    const box = await loc.boundingBox();
    if (!box || box.width < 1 || box.height < 1) continue;
    const vp = page.viewportSize() || { width: 1280, height: 800 };
    const clip = { x: Math.max(0, box.x), y: Math.max(0, box.y), width: Math.min(box.width, vp.width - Math.max(0, box.x)), height: Math.min(box.height, vp.height - Math.max(0, box.y)) };
    if (clip.width < 1 || clip.height < 1) continue;
    const style = await page.addStyleTag({ content: `${sel}, ${sel} *{color:transparent!important;-webkit-text-fill-color:transparent!important;text-shadow:none!important;caret-color:transparent!important}` });
    const png = await page.screenshot({ clip, animations: 'disabled' });
    await style.evaluate((n) => n.remove());
    const r = await page.evaluate(async ([b64, fg]) => {
      const blob = await (await fetch(`data:image/png;base64,${b64}`)).blob();
      const bmp = await createImageBitmap(blob);
      const c = new OffscreenCanvas(bmp.width, bmp.height); const g = c.getContext('2d'); g.drawImage(bmp, 0, 0);
      const d = g.getImageData(0, 0, bmp.width, bmp.height).data;
      const lum = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      const Lm = (a, b, c2) => 0.2126 * lum(a) + 0.7152 * lum(b) + 0.0722 * lum(c2);
      const ratios = [];
      for (let i = 0; i < d.length; i += 4) {
        const a = fg[3];
        const solid = [fg[0] * a + d[i] * (1 - a), fg[1] * a + d[i + 1] * (1 - a), fg[2] * a + d[i + 2] * (1 - a)];
        const x = Lm(...solid); const y = Lm(d[i], d[i + 1], d[i + 2]);
        ratios.push((Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05));
      }
      ratios.sort((p, q) => p - q);
      return { worst: ratios[Math.floor(ratios.length * 0.01)] };
    }, [png.toString('base64'), t.fg]);
    const large = t.px >= 24 || (t.px >= 18.66 && Number(t.weight) >= 700);
    const need = large ? 3 : 4.5;
    if (r.worst + 0.005 < need) out.push({ rule: 'color-contrast', wcag: '1.4.3', node: t.node, detail: `${r.worst.toFixed(2)}:1 < ${need}:1 measured on the rendered pixels behind the text (image or gradient background)` });
  }
  await page.evaluate(() => document.querySelectorAll('[data-a11y-px]').forEach((e) => e.removeAttribute('data-a11y-px')));
  return out;
}

/** Run the static checks over `scopeSelector` (default: the whole document). The result array has a `.stats` property. */
export async function staticAudit(page, scopeSelector = 'body') {
  const { out, stats, pixelTargets } = await inPage(page, staticInPage, { scope: scopeSelector, roles: [...ROLES] });
  const px = pixelTargets.length ? await pixelContrast(page, pixelTargets) : [];
  const all = [...out, ...px];
  Object.defineProperty(all, 'stats', { value: { ...stats, pixelChecked: pixelTargets.length } });
  return all;
}

/** Focus every focusable visible element in turn and check a focus indicator exists AND can be seen (2.4.7, 2.4.11, 1.4.11:
 *  the ring, shadow or colour change needs 3:1 against what it sits on). The result array has `.stats.focused`. */
export async function focusIndicatorAudit(page, scopeSelector = 'body', limit = 200) {
  const { out, focused } = await inPage(page, async (arg, L) => {
    const { parse, over, ratio, bgOf, sel } = L;
    const out = [];
    const scope = document.querySelector(arg.scope) || document.body;
    const sels = 'a[href],button:not([disabled]),input:not([type=hidden]):not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"]),[role=tab]';
    const els = [...scope.querySelectorAll(sels)].filter((e) => { if (e.closest('details:not([open])') && !e.matches('summary')) return false; const r = e.getBoundingClientRect(); const cs = getComputedStyle(e); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden'; }).slice(0, arg.cap);
    const surround = (el) => { const b = bgOf(el.parentElement || el); return b.unknown ? null : b.color; };
    const shadowRing = (v) => { const m = /(rgba?\([^)]+\)|color\(srgb[^)]+\))\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+([\d.]+)px\s*([\d.]+)?/.exec(v); return m && !/inset/.test(v) ? { c: parse(m[1]), blur: Number(m[4]), spread: Number(m[5] || 0) } : null; };
    let focused = 0;
    for (const el of els) {
      el.blur();
      const before = getComputedStyle(el);
      const b = { s: before.boxShadow, bc: before.borderColor, bg: before.backgroundColor, c: before.color };
      el.focus({ focusVisible: true });
      if (document.activeElement !== el) { out.push({ rule: 'focusable', wcag: '2.1.1', node: sel(el), detail: 'element did not take focus' }); continue; }
      focused += 1;
      const cs = getComputedStyle(el);
      const around = surround(el);
      const outlined = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 1 && cs.outlineColor !== 'transparent';
      const changed = cs.boxShadow !== b.s || cs.borderColor !== b.bc || cs.backgroundColor !== b.bg || cs.color !== b.c;
      let covered = false;
      const r = el.getBoundingClientRect();
      const top = document.elementFromPoint(Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1), Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1));
      if (r.top >= 0 && r.bottom <= innerHeight && top && !(el === top || el.contains(top) || top.contains(el))) covered = true;
      if (!outlined && !changed) out.push({ rule: 'focus-visible', wcag: '2.4.7', node: sel(el), detail: 'no visible focus indicator (outline/box-shadow/colour unchanged)' });
      else if (around) {
        // is the indicator itself visible? the best of: outline, box-shadow ring, border change, background change, colour change
        const candidates = [];
        if (outlined) { const oc = parse(cs.outlineColor); if (oc) candidates.push(['outline', ratio(over(oc, around), around)]); }
        const sh = shadowRing(cs.boxShadow);
        if (sh && sh.c && cs.boxShadow !== b.s && (sh.blur + sh.spread) >= 1) candidates.push(['box-shadow ring', ratio(over(sh.c, around), around)]);
        for (const [label, now, was] of [['border change', cs.borderColor, b.bc], ['background change', cs.backgroundColor, b.bg], ['text colour change', cs.color, b.c]]) {
          if (now === was) continue;
          const c1 = parse(now); const c0 = parse(was);
          if (c1 && c0) candidates.push([label, ratio(over(c1, around), over(c0, around))]);
        }
        const best = candidates.sort((x, y) => y[1] - x[1])[0];
        if (best && best[1] + 0.005 < 3) out.push({ rule: 'focus-indicator-contrast', wcag: '1.4.11', node: sel(el), detail: `the strongest focus indicator (${best[0]}) is ${best[1].toFixed(2)}:1, below 3:1, so it cannot be seen` });
      }
      // a control that takes focus but cannot be seen (transparent, or parked off screen without coming into view) is a keyboard trap for the eyes
      const r2 = el.getBoundingClientRect();
      let faded = 1; for (let e = el; e && e.nodeType === 1; e = e.parentElement) faded *= Number(getComputedStyle(e).opacity);
      if (faded < 0.1 || r2.right <= 0 || r2.bottom <= 0 || r2.left >= innerWidth) out.push({ rule: 'focus-visible', wcag: '2.4.7', node: sel(el), detail: faded < 0.1 ? 'focused element is (almost) transparent' : 'focused element is off screen' });
      if (covered) out.push({ rule: 'focus-not-obscured', wcag: '2.4.11', node: sel(el), detail: `focused element is covered by ${sel(top)}` });
      el.blur();
    }
    return { out, focused };
  }, { scope: scopeSelector, cap: limit });
  Object.defineProperty(out, 'stats', { value: { focused } });
  return out;
}

/** Press Tab through the page: no trap (focus keeps moving, leaves the app, never sticks) and a sensible reach. */
export async function tabTrapAudit(page, maxPresses = 300) {
  await page.evaluate(() => { document.activeElement?.blur?.(); window.scrollTo(0, 0); const s = document.createElement('span'); s.tabIndex = -1; document.body.prepend(s); s.focus(); s.remove(); });   // restart sequential focus navigation at the top
  const seen = [];
  let stuck = 0;
  let last = null;
  let leftPage = false;
  for (let i = 0; i < maxPresses; i += 1) {
    await page.keyboard.press('Tab');
    const id = await page.evaluate(() => {
      const a = document.activeElement;
      if (!a || a === document.body || a === document.documentElement) return 'BODY';
      const f = (a.id ? '#' + a.id : '') + (a.dataset && Object.keys(a.dataset).length && a.tagName !== 'DIV' ? `[data-${Object.keys(a.dataset)[0]}=${String(Object.values(a.dataset)[0]).slice(0, 30)}]` : '');
      return `${a.tagName.toLowerCase()}${f}|${(a.textContent || a.getAttribute('aria-label') || a.name || '').trim().slice(0, 20)}|${a.getAttribute('value') || ''}|${Math.round(a.getBoundingClientRect().top)}:${Math.round(a.getBoundingClientRect().left)}`;
    });
    const last0 = last;
    if (id === last) stuck += 1; else stuck = 0;
    last = id;
    if (stuck >= 3) return { trapped: true, at: id, seen };
    if (id === 'BODY') { leftPage = true; break; }
    if (id !== last0 && seen.includes(id)) { leftPage = true; break; }          // wrapped round to the start: a loop through the page, not a trap
    seen.push(id);
  }
  return { trapped: !leftPage && seen.length >= maxPresses, at: last, seen, wrapped: leftPage };
}

/** Horizontal overflow of the document (1.4.10): which elements stick out past the viewport. */
export async function overflowAudit(page) {
  return page.evaluate(() => {
    const de = document.documentElement;
    const out = { scrollWidth: de.scrollWidth, clientWidth: de.clientWidth, offenders: [], clipped: [] };
    // 1.4.10 is also lost when content is CUT instead of scrolled: a box with overflow hidden/clip whose content is wider than the box
    // (an ellipsis is a deliberate, visible cut and is allowed; the page never scrolls, so the document check above cannot see this)
    for (const el of document.body.querySelectorAll('*')) {
      const cs = getComputedStyle(el);
      if (!['hidden', 'clip'].includes(cs.overflowX) || cs.textOverflow === 'ellipsis' || cs.display === 'none' || cs.visibility === 'hidden') continue;
      if (el.closest('svg') || el.clientWidth === 0) continue;
      if (el.scrollWidth > el.clientWidth + 1) out.clipped.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${el.classList.length ? '.' + [...el.classList].slice(0, 2).join('.') : ''} content ${el.scrollWidth}px in ${el.clientWidth}px`);
    }
    out.clipped = out.clipped.slice(0, 8);
    if (de.scrollWidth > de.clientWidth + 1) {
      for (const el of document.body.querySelectorAll('*')) {
        const r = el.getBoundingClientRect();
        if (r.right > de.clientWidth + 1 && r.width > 0 && getComputedStyle(el).position !== 'fixed') {
          // clipped by its own scroll container is fine (tables/code may scroll internally)
          let clipped = false;
          for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
            const ox = getComputedStyle(p).overflowX;
            if (ox !== 'visible' && p.getBoundingClientRect().right <= de.clientWidth + 1) { clipped = true; break; }
          }
          if (!clipped) out.offenders.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${el.classList.length ? '.' + [...el.classList].slice(0, 2).join('.') : ''} right=${Math.round(r.right)}`);
        }
      }
      out.offenders = out.offenders.slice(0, 8);
    }
    return out;
  });
}

/** The one-line verdict on an overflowAudit result: the page scrolls sideways, or some box cuts its content. '' when fine. */
export function overflowFailure(o) {
  const parts = [];
  if (o.scrollWidth > o.clientWidth + 1) parts.push(`scrollWidth ${o.scrollWidth} > ${o.clientWidth}: ${o.offenders.join(', ')}`);
  if (o.clipped.length) parts.push(`content is cut off: ${o.clipped.join(', ')}`);
  return parts.join('; ');
}

/** Reduced motion: with `prefers-reduced-motion: reduce` nothing animates or transitions for longer than 1ms. */
export async function motionAudit(page) {
  return page.evaluate(() => {
    const out = [];
    const running = document.getAnimations().filter((a) => a.playState === 'running' && a.effect);
    for (const a of running) {
      const t = a.effect.target;
      const timing = a.effect.getComputedTiming();
      if (timing.duration > 1) out.push({ rule: 'reduced-motion', wcag: '2.3.3', node: t ? `${t.tagName.toLowerCase()}.${[...t.classList].join('.')}` : '?', detail: `${a.constructor.name} ${a.animationName || a.transitionProperty || ''} ${Math.round(timing.duration)}ms still runs` });
    }
    for (const el of document.querySelectorAll('*')) {
      const cs = getComputedStyle(el);
      const dur = (v) => Math.max(...v.split(',').map((x) => (x.trim().endsWith('ms') ? parseFloat(x) : parseFloat(x) * 1000) || 0));
      if (dur(cs.animationDuration) > 1 && cs.animationName !== 'none') out.push({ rule: 'reduced-motion', wcag: '2.3.3', node: el.tagName.toLowerCase() + '.' + [...el.classList].join('.'), detail: `animation ${cs.animationName} ${cs.animationDuration}` });
      if (dur(cs.transitionDuration) > 1 && cs.transitionProperty !== 'none') out.push({ rule: 'reduced-motion', wcag: '2.3.3', node: el.tagName.toLowerCase() + '.' + [...el.classList].join('.'), detail: `transition ${cs.transitionProperty} ${cs.transitionDuration}` });
      if (cs.scrollBehavior === 'smooth') out.push({ rule: 'reduced-motion', wcag: '2.3.3', node: el.tagName.toLowerCase(), detail: 'smooth scrolling' });
    }
    return out;
  });
}

/** Group findings by rule for compact assertion messages. */
export function summarize(findings) {
  const by = new Map();
  for (const f of findings) { const a = by.get(f.rule) || []; a.push(f); by.set(f.rule, a); }
  return [...by].map(([rule, list]) => `${rule} (${list.length}): ` + list.slice(0, 4).map((f) => `${f.node} — ${f.detail}`).join(' | ')).join('\n');
}

/**
 * Dynamic live-region check (4.1.3): run `trigger()` (a click, a keypress, a response arriving) and report every piece of visible text
 * that CHANGED but is not inside a live region (role=status/alert/log or aria-live other than off). A message a screen-reader user is
 * never told about is the usual failure. `within` limits the watch to one subtree. The result array has `.stats.changed`.
 */
export async function liveAnnouncementAudit(page, trigger, within = 'body') {
  await page.evaluate((sel) => {
    window.__liveChanges = [];
    const root = document.querySelector(sel) || document.body;
    window.__liveObs?.disconnect();
    window.__liveObs = new MutationObserver((list) => {
      for (const m of list) {
        const el = m.type === 'characterData' ? m.target.parentElement : m.target;
        if (el && el.nodeType === 1) window.__liveChanges.push(el);
      }
    });
    window.__liveObs.observe(root, { childList: true, characterData: true, subtree: true });
  }, within);
  await trigger();
  await page.waitForTimeout(300);
  const res = await page.evaluate(() => {
    window.__liveObs.disconnect();
    const live = (el) => el.closest('[role=status],[role=alert],[role=log],[aria-live]:not([aria-live=off])');
    const sel = (el) => `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${el.classList.length ? '.' + [...el.classList].slice(0, 2).join('.') : ''}`;
    const seen = new Set(); const out = []; let changed = 0;
    for (const el of window.__liveChanges) {
      if (seen.has(el) || !el.isConnected) continue;
      seen.add(el);
      const text = (el.textContent || '').trim();
      if (!text) continue;
      const cs = getComputedStyle(el); const r = el.getBoundingClientRect();
      if (cs.visibility === 'hidden' || cs.display === 'none' || r.width === 0 || r.height === 0) continue;
      if (el.closest('svg') || el.closest('[aria-hidden=true]')) continue;
      changed += 1;
      if (!live(el)) out.push({ rule: 'live-announcement', wcag: '4.1.3', node: sel(el), detail: `text changed to "${text.slice(0, 60)}" but nothing announces it (not in a live region)` });
    }
    return { out, changed };
  });
  Object.defineProperty(res.out, 'stats', { value: { changed: res.changed } });
  return res.out;
}
