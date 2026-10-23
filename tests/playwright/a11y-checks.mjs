// In-house WCAG 2.2 AA spot checks for the HerdR collaboration page (T16 UI gates).
//
// This repo takes no new dependencies (DESIGN.md: "VanillaJS... no new dependencies"; package.json carries only
// playwright), so axe-core is NOT used. These checks are a deliberately small, deterministic subset and are NOT a
// conformance claim: automated checks decide only a minority of the AA criteria (see autospec-design
// gate.review-not-auto-merged). Each check returns a list of findings {rule, wcag, node, detail}.
//
// Everything runs inside the page (page.evaluate), against computed styles and the live accessibility-relevant DOM.

/** Run the static checks over `scopeSelector` (default: the whole document). */
export async function staticAudit(page, scopeSelector = 'body') {
  return page.evaluate((scopeSel) => {
    const out = [];
    const scope = document.querySelector(scopeSel) || document.body;
    const sel = (el) => {
      if (!(el instanceof Element)) return String(el);
      const id = el.id ? `#${el.id}` : '';
      const cls = el.classList.length ? '.' + [...el.classList].slice(0, 2).join('.') : '';
      const data = [...el.attributes].filter((a) => a.name.startsWith('data-')).slice(0, 1).map((a) => `[${a.name}="${String(a.value).slice(0, 24)}"]`).join('');
      return `${el.tagName.toLowerCase()}${id}${cls}${data}`;
    };
    const add = (rule, wcag, el, detail) => out.push({ rule, wcag, node: sel(el), detail });
    const visible = (el) => {
      for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
        const cs = getComputedStyle(e);
        if (cs.display === 'none' || cs.visibility === 'hidden' || e.hidden || e.getAttribute('aria-hidden') === 'true') return false;
      }
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    };
    const all = [...scope.querySelectorAll('*')].filter((e) => !['SCRIPT', 'STYLE', 'LINK', 'META', 'TITLE', 'HEAD'].includes(e.tagName));

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
    const interactive = all.filter((el) => {
      if (!visible(el)) return false;
      const t = el.tagName.toLowerCase();
      const role = el.getAttribute('role');
      if (['button', 'select', 'textarea', 'summary'].includes(t)) return true;
      if (t === 'a' && el.hasAttribute('href')) return true;
      if (t === 'input' && el.type !== 'hidden') return true;
      if (role && ['button', 'tab', 'link', 'checkbox', 'radio', 'switch', 'menuitem', 'option', 'textbox', 'combobox', 'slider', 'treeitem'].includes(role)) return true;
      if (el.hasAttribute('tabindex') && el.tabIndex >= 0 && !['body'].includes(t)) return true;
      return false;
    });
    for (const el of interactive) {
      if (!nameOf(el)) add('name-role-value', '4.1.2', el, 'interactive element has no accessible name');
    }
    // images / svg graphics that are exposed
    for (const el of all) {
      if (el.tagName === 'IMG' && !el.hasAttribute('alt') && el.getAttribute('role') !== 'presentation') add('image-alt', '1.1.1', el, 'img without alt');
      if (el.getAttribute('role') === 'img' && !nameOf(el)) add('image-alt', '1.1.1', el, 'role=img without a name');
    }
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
    // ---- headings: one h1, no skipped levels downward from first
    const hs = [...scope.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(visible).map((h) => Number(h.tagName[1]));
    for (let i = 1; i < hs.length; i += 1) if (hs[i] - hs[i - 1] > 1) { add('heading-order', '1.3.1', scope.querySelectorAll('h1,h2,h3,h4,h5,h6')[i], `h${hs[i - 1]} followed by h${hs[i]}`); break; }
    // ---- landmarks / language / title / zoom
    if (!document.documentElement.getAttribute('lang')) add('html-lang', '3.1.1', document.documentElement, 'no lang on <html>');
    if (!document.title.trim()) add('document-title', '2.4.2', document.documentElement, 'empty <title>');
    const vp = document.querySelector('meta[name=viewport]');
    if (vp && /user-scalable\s*=\s*(no|0)|maximum-scale\s*=\s*1(\.0)?\b/i.test(vp.content)) add('zoom-not-blocked', '1.4.4', vp, 'viewport blocks zoom');
    if (!vp) add('viewport-meta', '1.4.10', document.documentElement, 'no viewport meta');
    // ---- form controls need a visible/programmatic label
    for (const el of scope.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea')) {
      if (visible(el) && !nameOf(el)) add('label', '3.3.2', el, 'form control without a label');
    }

    // ---- contrast of rendered text (WCAG 1.4.3). Backgrounds that are images/gradients are reported, not guessed.
    const parse = (c) => {
      let m = /^rgba?\(([^)]+)\)$/.exec(c);
      if (m) { const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; }
      m = /^color\(srgb ([^)]+)\)$/.exec(c);
      if (m) { const p = m[1].split(/[ \/]+/).filter(Boolean).map(Number); return [p[0] * 255, p[1] * 255, p[2] * 255, p.length > 3 ? p[3] : 1]; }
      return null;
    };
    const over = (top, bottom) => { const a = top[3]; return [top[0] * a + bottom[0] * (1 - a), top[1] * a + bottom[1] * (1 - a), top[2] * a + bottom[2] * (1 - a), 1]; };
    const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
    const ratio = (a, b) => { const x = lum(a); const y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
    const bgOf = (el) => {
      const stack = [];
      for (let e = el; e; e = e.parentElement) {
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
    const seen = new Set();
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    let tn;
    const contrastFails = new Map();
    while ((tn = walker.nextNode())) {
      if (!tn.nodeValue.trim()) continue;
      const el = tn.parentElement;
      if (!el || seen.has(el) || !visible(el) || el.closest('script,style,svg')) continue;
      seen.add(el);
      if (el.closest('[disabled],[aria-disabled=true]')) continue;       // inactive components are exempt (1.4.3)
      const cs = getComputedStyle(el);
      const fg = parse(cs.color);
      const bg = bgOf(el);
      if (!fg || bg.unknown) continue;
      const solid = over([fg[0], fg[1], fg[2], fg[3] * (Number(cs.opacity) || 1)], bg.color);
      const r = ratio(solid, bg.color);
      const px = parseFloat(cs.fontSize);
      const large = px >= 24 || (px >= 18.66 && Number(cs.fontWeight) >= 700);
      const need = large ? 3 : 4.5;
      if (r + 0.005 < need) {
        const key = `${cs.color}|${bg.color.map(Math.round)}|${large}`;
        const hit = contrastFails.get(key);
        if (hit) hit.n += 1;
        else contrastFails.set(key, { el, r, need, n: 1, fg: cs.color, bg: bg.color.map(Math.round).join(',') });
      }
    }
    for (const f of contrastFails.values()) add('color-contrast', '1.4.3', f.el, `${f.r.toFixed(2)}:1 < ${f.need}:1 (fg ${f.fg} on rgb(${f.bg})), ${f.n} text node(s)`);

    // ---- non-text contrast of focus/state is covered by the focus check below; target size (2.5.8, 24 CSS px)
    for (const el of interactive) {
      const t = el.tagName.toLowerCase();
      if (t === 'input' && ['checkbox', 'radio'].includes(el.type)) continue;      // native control; its label is the target
      if (t === 'a' && el.closest('p,li,span,small,label') && getComputedStyle(el).display === 'inline') continue;   // inline in text (exception)
      const r = el.getBoundingClientRect();
      if (r.width + 0.5 < 24 || r.height + 0.5 < 24) {
        // spacing exception: a 24px circle on the target's centre must not touch another target's box
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
    // ---- unlabeled live-region misuse: containers announcing every change should be small
    for (const el of scope.querySelectorAll('[aria-live=assertive]')) add('live-assertive', '4.1.3', el, 'assertive live region (interrupts); prefer polite/role=status');
    return out;
  }, scopeSelector);
}

/** Focus every focusable visible element in turn and check a focus indicator exists (2.4.7 / 2.4.11-13). */
export async function focusIndicatorAudit(page, scopeSelector = 'body', limit = 200) {
  return page.evaluate(async ([scopeSel, cap]) => {
    const out = [];
    const scope = document.querySelector(scopeSel) || document.body;
    const sels = 'a[href],button:not([disabled]),input:not([type=hidden]):not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"]),[role=tab]';
    const els = [...scope.querySelectorAll(sels)].filter((e) => { if (e.closest("details:not([open])") && !e.matches("summary")) return false; const r = e.getBoundingClientRect(); const cs = getComputedStyle(e); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden'; }).slice(0, cap);
    const sel = (el) => `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${el.classList.length ? '.' + [...el.classList].slice(0, 2).join('.') : ''}`;
    for (const el of els) {
      el.blur();
      const before = getComputedStyle(el);
      const b = { o: before.outlineStyle + before.outlineWidth, s: before.boxShadow, bc: before.borderColor, bg: before.backgroundColor, c: before.color };
      el.focus({ focusVisible: true });
      if (document.activeElement !== el) { out.push({ rule: 'focusable', wcag: '2.1.1', node: sel(el), detail: 'element did not take focus' }); continue; }
      const cs = getComputedStyle(el);
      const outlined = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 1 && cs.outlineColor !== 'transparent';
      const changed = cs.boxShadow !== b.s || cs.borderColor !== b.bc || cs.backgroundColor !== b.bg || cs.color !== b.c;
      let covered = false;
      const r = el.getBoundingClientRect();
      const top = document.elementFromPoint(Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1), Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1));
      if (r.top >= 0 && r.bottom <= innerHeight && top && !(el === top || el.contains(top) || top.contains(el))) covered = true;
      if (!outlined && !changed) out.push({ rule: 'focus-visible', wcag: '2.4.7', node: sel(el), detail: 'no visible focus indicator (outline/box-shadow/colour unchanged)' });
      if (covered) out.push({ rule: 'focus-not-obscured', wcag: '2.4.11', node: sel(el), detail: `focused element is covered by ${sel(top)}` });
      el.blur();
    }
    return out;
  }, [scopeSelector, limit]);
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
    const out = { scrollWidth: de.scrollWidth, clientWidth: de.clientWidth, offenders: [] };
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
      if (dur(cs.transitionDuration) > 1 && cs.transitionProperty !== 'none' && cs.transitionProperty !== 'all') out.push({ rule: 'reduced-motion', wcag: '2.3.3', node: el.tagName.toLowerCase() + '.' + [...el.classList].join('.'), detail: `transition ${cs.transitionProperty} ${cs.transitionDuration}` });
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
