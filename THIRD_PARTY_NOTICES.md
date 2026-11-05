# Third-party notices

## CodeBoarding (MIT)

- Upstream: https://github.com/CodeBoarding/CodeBoarding, pinned at commit
  `647fe6a728169ac442ae24a5feeba50784ea2231` (v0.15.2).
- Vendored: `herdr_engineering/architecture/_vendor/codeboarding_content_hash.py`
  (from `agents/content_hash.py`, pure hashing functions only). The upstream
  copyright and permission notice is reproduced at the top of that file.
- Not vendored (rejected, see `docs/specs/herdr-codeboarding-native-port-2026-10-23/audit.md`):
  telemetry (PostHog), tool installers, remote/upload workflows, LLM agents,
  the pickle-based analysis cache.

Copyright (c) 2025 CodeBoarding. Permission is granted under the MIT License
text in the vendored file header.

## pdf.js (Apache-2.0)

- Upstream: https://github.com/mozilla/pdf.js, npm package pdfjs-dist 4.10.38 (tarball sha512 checked against the
  registry's `dist.integrity` by `scripts/vendor-pdfjs.sh`).
- Vendored, unmodified: `herdr_engineering/control_plane/share_static/vendor/pdfjs/pdf.min.mjs`,
  `pdf.worker.min.mjs`, the upstream `LICENSE`, and `SHA256SUMS` (checked by `tests/control_plane/test_pdfjs_vendor.py`).
  Both the dashboard and the share-public viewer serve these files.
- Not vendored: `cmaps/`, `standard_fonts/` and `wasm/`, which pdf.js fetches on demand (CJK and non-embedded-font PDFs,
  JPEG2000 and some image decoders may render poorly or not at all).

Copyright Mozilla Foundation and contributors; licensed under the Apache License, Version 2.0 (see `LICENSE` there).
