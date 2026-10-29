# PDF.js browser distribution

Source: https://github.com/mozilla/pdf.js / https://www.npmjs.com/package/pdfjs-dist
Pinned npm package: **pdfjs-dist 6.4.299**, Apache-2.0 (see LICENSE).

`pdf.mjs` and `pdf.worker.mjs` are byte-for-byte copies of `build/pdf.mjs`
and `build/pdf.worker.mjs` in the installed pinned package. `standard_fonts/`
is copied verbatim, including the Foxit and Liberation font licenses. Upstream
copyright and license headers are retained. No viewer, scripting sandbox,
annotation editor, external CDN, or document JavaScript engine is included.

Reproduce from the locked npm install:

```
cp node_modules/pdfjs-dist/build/pdf.mjs node_modules/pdfjs-dist/build/pdf.worker.mjs node_modules/pdfjs-dist/LICENSE packages/vendor/pdfjs/
cp node_modules/pdfjs-dist/standard_fonts/* packages/vendor/pdfjs/standard_fonts/
```

Integration reference: https://mozilla.github.io/pdf.js/examples/

The trusted adapter supplies fetched bytes to an explicitly owned module worker
(no PDF.js fake-worker fallback). It renders only canvas images and literal text.
XFA, font-face injection, WASM, annotation layers and document scripting are not
enabled. CSP retains self-only scripts without eval/WASM permissions. CMaps and
optional WASM codecs are not shipped; documents requiring those assets can fail
with the visible Download fallback. Fonts are restricted to the copied static
assets. The npm optional native canvas package is not shipped to browsers.

On upgrade, rerun byte-identity, public asset/import-graph, and Chromium tests;
review upstream licensing/security changes before replacing these files.

SHA-256 of the unmodified browser bundles:

- `pdf.mjs`: `dd13af821c9c3e1778653340d6539a1ba07bc56cba40073a4c727864cf0c232a`
- `pdf.worker.mjs`: `07ceb740d746e5d9012fcf9f27d5c7dea09f7d2f5e74f5c8109e5787551a2333`
