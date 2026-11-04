# Cross-fabric authoring and Claude skill distillation

## Goal
Any authorized agent creates/edits an artifact from any fabric host without creator session still existing. Preserve original editable source and build recipes. Build jobs can run on other eligible hosts and hand back exact version IDs. Existing fabric resource scheduler chooses suitable host by capability/load, honors host denylist and owner priority.

## Upstream review
Audit Anthropic `skills` repository `web-artifacts-builder` and relevant document/presentation skills. Record precise upstream revision and license per subtree/dependency, not just repository root. Distill concepts and workflow; independently implement where license not permissive. Do not assume all Anthropic skills are MIT-licensed. Keep notices for code actually reused. No hard dependency on Claude, cloud-hosted artifact service, or proprietary generator API.

## Skill interface
One `herdr-artifact` skill available to Pi, Claude Code and external compatible agents by CLI-contract wrappers. Commands: `create`, `checkout`, `inspect`, `edit`, `validate`, `publish --base-version`, `history`, `diff`, `materialize`, `bundle`, `share-request`. Formats: `web` (React/TS/HTML), `document` (Markdown/DOCX/PDF outputs), `diagram` (Mermaid/SVG), `visualization` (JSON/chart/HTML), `presentation`, `report`. Use renderer adapters and templates; absence of adapter must give structured unsupported-format result.

## Build job contract
Inputs: artifact ID (optional), desired profile, source input artifact references and versions, request/instructions, source repo ref, template, requester identity, desired renderer capabilities, base version, intended output types. Outputs: immutable source archive, manifest, output renditions, previews/screenshots, toolchain metadata/lockfiles, validation report, logs, provenance links. Logs should redact credentials, prompts and personal content according to policy. Job lifecycle persisted; retry/failover where safe. Sandboxed network default off, allowlists as required; CPU/RAM/disk/time/egress limits.

## Agent reuse
Search authorized registry before creating similarly named artifacts. Provide candidate reuse/diff suggestions; never auto-edit somebody else's artifact without authorization. On edit, use checkout in temp isolated directory, verify hash, inspect manifest/source, apply modification, run tests/screenshot validation, publish only on successful CAS; leave rejected versions for inspection. Attach relevant mission/room/PR/issue IDs using existing link model. No automatic public sharing.

## Dependency handling
Track source dependencies via immutable artifact version IDs and git SHA/graph snapshots. Detect staleness and notify; rebuild only under policy/human approval, not automatically overwrite published public content. Artifact collections bundle manifests of pinned version IDs; deterministic ZIP export. No inaccessible local Mac Downloads references inside generated artifact metadata.
