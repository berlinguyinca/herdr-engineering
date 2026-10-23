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
