// Light/dark/auto and calm on/off/auto controls, rendered into the app shell's "controls" slot.
import { setCalm } from './motion.js';

const THEME_KEY = 'herdr.theme';

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

const svg = (inner) => `<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
const ICONS = {
  theme: { auto: svg('<circle cx="10" cy="10" r="6.5"/><path d="M10 3.5v13" /><path d="M10 3.5a6.5 6.5 0 0 1 0 13z" fill="currentColor"/>'),
    light: svg('<circle cx="10" cy="10" r="3.2"/><path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4"/>'),
    dark: svg('<path d="M16 11.5A6.5 6.5 0 0 1 8.5 4a6.5 6.5 0 1 0 7.5 7.5z"/>') },
  calm: { auto: svg('<path d="M2.5 10c2-3 4-3 5 0s3 3 5 0 3-3 5 0"/><path d="M10 3v2" opacity=".5"/>'),
    on: svg('<path d="M2.5 10h15"/>'),
    off: svg('<path d="M2.5 11c1.5-5 3-5 4 0s2.500 5 4 0 2.500-5 4 0 1.500 3 2.500 0"/>') },
};

// A button that steps through its values; the tooltip and accessible name say where it is and what a click does.
function cycle(label, name, options, current, onChange) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'hd-icon-btn';
  button.dataset.control = name;
  let value = current;
  const paint = () => {
    const next = options[(options.indexOf(value) + 1) % options.length];
    const text = `${label}: ${value}. Switch to ${next}`;
    button.setAttribute('aria-label', text); button.title = text;
    button.dataset.value = value;
    button.innerHTML = ICONS[name][value];
  };
  button.addEventListener('click', () => { value = options[(options.indexOf(value) + 1) % options.length]; onChange(value); paint(); });
  paint();
  return button;
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
    cycle('Theme', 'theme', ['auto', 'light', 'dark'], theme, applyTheme),
    cycle('Calm', 'calm', ['auto', 'on', 'off'], calm, setCalm),
  );
  shell.append(box);
}
