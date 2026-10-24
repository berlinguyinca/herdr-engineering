export const PDF_LIMIT = 20 * 1024 * 1024;

// Only fetched, bounded PDF bytes enter the browser's viewer. No HTML fallback,
// plugin object, remote embed, script permissions or persistent blob storage.
export function renderPdf(bytes) {
  if (new TextDecoder().decode(bytes.slice(0, 5)) !== '%PDF-') throw new Error('Invalid PDF signature.');
  const host = document.createElement('div'), frame = document.createElement('iframe'), note = document.createElement('p');
  const url = URL.createObjectURL(new Blob([bytes], { type: 'application/pdf' }));
  frame.title = 'PDF preview'; frame.setAttribute('sandbox', ''); frame.referrerPolicy = 'no-referrer';
  frame.style.cssText = 'width:100%;height:520px;border:1px solid #888;min-width:0'; frame.src = url;
  note.textContent = 'PDF page navigation and search depend on your browser. If its PDF viewer is blocked by the sandbox, use Download to view locally.';
  host.append(note, frame);
  host.dispose = () => { frame.removeAttribute('src'); URL.revokeObjectURL(url); };
  return host;
}
