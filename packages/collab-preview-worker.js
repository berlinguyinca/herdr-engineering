import { textPreviewMarkup, boundedText, TEXT_LIMIT } from './collab-preview-text.js';
import { parseMsp } from './collab-preview-msp.js';

// One request per worker. Only bounded source bytes cross this boundary: no
// room credentials, object URLs, DOM access or document-supplied code.
self.onmessage = ({ data: { kind, bytes } }) => {
  try {
    if (!(bytes instanceof Uint8Array) || bytes.byteLength > TEXT_LIMIT) throw new Error('Preview text byte limit exceeded.');
    const text = boundedText(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
    const result = kind === 'msp'
      ? { kind, spectra: parseMsp(text) }
      : { kind, markup: textPreviewMarkup(kind, text) };
    self.postMessage({ result });
  } catch (error) {
    self.postMessage({ error: error.message });
  }
};
