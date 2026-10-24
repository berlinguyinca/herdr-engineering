// Keep stored preferences bounded to the supported presentation modes.
export const DENSITIES = ['comfortable', 'compact', 'dense'];
export function sanitizeDensity(value) {
  return DENSITIES.includes(value) ? value : 'comfortable';
}
