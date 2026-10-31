// The only script of our own views (allowed by the response's CSP nonce). It builds nothing from markup: every node is
// created with createElement and filled through textContent or a property, so no content or annotation text can be
// parsed as HTML. This file holds the live-mode rendering only; the annotation panel arrives with the annotation view.
const dataNode = document.getElementById('herdr-view-data');
const data = dataNode ? JSON.parse(dataNode.textContent) : null;
const host = document.getElementById('herdr-native');
if (data && host) {
  if (data.kind === 'image') {
    const img = document.createElement('img');
    img.alt = data.file;
    img.src = data.raw_href;
    host.append(img);
  } else if (data.kind === 'pdf' || data.kind === 'document') {
    const link = document.createElement('a');
    link.href = data.raw_href;
    link.rel = 'noopener';
    link.textContent = data.kind === 'pdf' ? 'Open the PDF' : 'Download ' + data.file;
    if (data.kind === 'document') link.download = '';
    host.append(link);
  }
}
