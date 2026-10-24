// Light/dark/auto and calm on/off/auto controls, rendered into the app shell's "controls" slot.
import { setCalm } from './motion.js';

const THEME_KEY = 'herdr.theme';
const CALM_KEY = 'herdr.calm';

export function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'light' || mode === 'dark') root.dataset.theme = mode;
  else delete root.dataset.theme;
  try {
    if (mode === 'light' || mode === 'dark') localStorage.setItem(THEME_KEY, mode);
    else localStorage.removeItem(THEME_KEY);
  } catch { /* storage may be unavailable */ }
}

export function loadTheme() {
  let mode = null;
  try { mode = localStorage.getItem(THEME_KEY); } catch { /* ignore */ }
  const root = document.documentElement;
  if (mode === 'light' || mode === 'dark') root.dataset.theme = mode;
  else delete root.dataset.theme;
  return mode === 'light' || mode === 'dark' ? mode : 'auto';
}

function select(label, name, options, current, onChange) {
  const wrap = document.createElement('label');
  wrap.append(label + ' ');
  const sel = document.createElement('select');
  sel.setAttribute('aria-label', label);
  sel.dataset.control = name;
  for (const o of options) {
    const opt = document.createElement('option');
    opt.value = o; opt.textContent = o[0].toUpperCase() + o.slice(1);
    if (o === current) opt.selected = true;
    sel.append(opt);
  }
  sel.addEventListener('change', () => onChange(sel.value));
  wrap.append(sel);
  return wrap;
}

export function mountThemeControls(shell) {
  if (!shell || shell.querySelector('.hd-controls')) return;
  const root = document.documentElement;
  const theme = root.dataset.theme === 'light' || root.dataset.theme === 'dark' ? root.dataset.theme : 'auto';
  const calm = root.dataset.calm === 'on' || root.dataset.calm === 'off' ? root.dataset.calm : 'auto';
  const box = document.createElement('div');
  box.className = 'hd-controls';
  box.slot = 'controls';
  box.append(
    select('Theme', 'theme', ['auto', 'light', 'dark'], theme, applyTheme),
    select('Calm', 'calm', ['auto', 'on', 'off'], calm, setCalm),
  );
  shell.append(box);
}

export { CALM_KEY };
