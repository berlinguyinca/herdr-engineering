# Canonical source

The distributable cross-harness skill lives in:

<https://github.com/berlinguyinca/herdr-artifacts>

This repository keeps a vendored copy under `skills/herdr-artifact` so the
HerdR control-plane checkout is self-contained during development and image
builds. Update the standalone repository first, validate it, then synchronize
this copy and record the upstream commit here.

The current local synchronization source is the standalone repository commit
that contains the matching `SKILL.md` and `scripts/herdr-artifact`. Do not copy
Anthropic/Claude source or assets into either repository.
