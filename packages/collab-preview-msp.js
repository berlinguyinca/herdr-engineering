import { escapeHtml as esc } from './herdr-web-components/util.js';
import { boundedText, sandboxMarkup } from './collab-preview-text.js';

export function parseMsp(value) {
  const lines = boundedText(value).split(/\r?\n/), spectra = [];
  let spectrum = null, count = null, total = 0;
  const finish = () => {
    if (!spectrum) return;
    if (!spectrum.peaks.length || (count !== null && count !== spectrum.peaks.length)) throw new Error('Invalid MSP peak count.');
    spectra.push(spectrum);
    if (spectra.length > 100) throw new Error('MSP spectrum limit exceeded.');
  };
  for (const raw of lines) {
    const line = raw.trim(); if (!line) continue;
    const field = /^([A-Za-z][^:]*):\s*(.*)$/.exec(line);
    if (field) {
      if (field[1].toLowerCase() === 'name') {
        finish(); spectrum = { name: field[2], metadata: [], peaks: [] }; count = null;
      }
      if (!spectrum) throw new Error('Invalid MSP: expected Name.');
      spectrum.metadata.push([field[1], field[2]]);
      if (/^num peaks$/i.test(field[1])) {
        count = Number(field[2]);
        if (!Number.isSafeInteger(count) || count < 1 || count > 10000) throw new Error('Invalid MSP peak count.');
      }
      continue;
    }
    if (!spectrum) throw new Error('Invalid MSP: expected Name.');
    for (const peak of line.split(/;\s*(?=[\d.+-])/)) {
      const match = /^([\d.eE+-]+)\s+([\d.eE+-]+)(?:\s+"[^"\r\n]*")?\s*;?$/.exec(peak);
      const mz = Number(match?.[1]), intensity = Number(match?.[2]);
      if (!match || !Number.isFinite(mz) || !Number.isFinite(intensity) || mz <= 0 || intensity < 0) throw new Error('Invalid MSP peak.');
      if (++total > 10000) throw new Error('MSP peak limit exceeded.');
      spectrum.peaks.push([mz, intensity]);
    }
  }
  finish();
  if (!spectra.length) throw new Error('Invalid MSP: no spectra.');
  return spectra;
}

// Numeric coordinates only; labels pass through escaping. SVG titles expose
// exact point values on hover, with a native selector for library/trace browsing.
export function plotMarkup(name, points, { spectrum = false } = {}) {
  let maxX = 1, maxY = 1;
  for (const [x, y] of points) { maxX = Math.max(maxX, x); maxY = Math.max(maxY, y); }
  const xy = ([x, y]) => [40 + x / maxX * 540, 250 - y / maxY * 220];
  const marks = spectrum ? points.map(p => {
    const [x, y] = xy(p);
    return `<line data-peak x1="${x}" y1="250" x2="${x}" y2="${y}" stroke="#175aaa" stroke-width="2"><title>m/z ${p[0]}, intensity ${p[1]}</title></line>`;
  }).join('') : `<polyline fill="none" stroke="#175aaa" stroke-width="2" points="${points.map(p => xy(p).join(',')).join(' ')}"><title>${esc(name)}</title></polyline>`;
  return `<h2>${esc(name)}</h2><svg viewBox="0 0 620 290" role="img" aria-label="${spectrum ? 'Mass spectrum' : 'Chromatogram'}"><path d="M40 20V250H600" fill="none" stroke="currentColor"/>${marks}<text x="280" y="280">${spectrum ? 'm/z' : 'Retention time'}</text><text x="45" y="18">Intensity (max ${maxY})</text><text x="540" y="268">${maxX}</text></svg>`;
}

export function renderMsp(text) {
  const spectra = parseMsp(text), host = document.createElement('div');
  const label = document.createElement('label'), select = document.createElement('select');
  label.textContent = 'Spectrum '; select.setAttribute('aria-label', 'Spectrum'); label.append(select);
  spectra.forEach((s, i) => { const option = document.createElement('option'); option.value = String(i); option.textContent = s.name; select.append(option); });
  const display = document.createElement('div'); host.append(label, display);
  const draw = () => {
    const s = spectra[Number(select.value)];
    display.replaceChildren(sandboxMarkup(plotMarkup(s.name, s.peaks, { spectrum: true }) + `<dl>${s.metadata.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`, 'MSP spectrum preview'));
  };
  select.onchange = draw; draw(); return host;
}
