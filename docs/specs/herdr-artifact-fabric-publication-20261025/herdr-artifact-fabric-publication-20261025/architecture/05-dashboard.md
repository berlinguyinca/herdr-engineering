# Dashboard UX / integration

## IA
Add top-level `Artifacts` and `Publications` views to existing dashboard navigation design, retain current typography, spacing, theme, table, drawer and graph patterns. Contextual tabs on workspace, mission, session, collaboration room, agent and graph node show same authorized artifact records filtered by binding. Avoid new UI framework and duplicate composer.

## Artifacts list
Search query, type, workspace, host provenance, creator, session, dates, tags, visibility and lifecycle filters; server-side paginated results with ACL enforced before aggregation. Table/grid toggles, thumbnails, size, version, processing state. Empty/loading/error/offline states. Keyboard navigation and accessible metadata.

## Detail split-pane
Main preview and right-side metadata/version sidebar. PDF page viewer; safe image zoom; Markdown rendered/raw toggle; JSON tree/raw; DOCX converted rendition; static source text; sanitized diagrams; HTML preview sandboxed cross-origin; unsupported → safe download. Tabs: Preview, Source (authorized), Versions/Diff, Relationships, Comments/Review, Sharing, Build logs (authorized). Agent action creates a new job/request, not browser-side arbitrary execution.

## Collaborations
Reuse `conversation-composer.js` attachment adapter, fill `upload` and message refs via existing collab transport; render artifact chips, no binary payload in event stream. Session workspace and room share identical artifact reference contract. Mark immutable pinned vs latest links visibly. Graph nodes surface linked artifacts and click through to viewer.

## Publications list
Resources, public URLs, audience, provider, status, expiry, request counts/errors, owner, approval, actions revoke/extend/suspend. Detail drawer shows actual vs desired state, audit, authentication policy and public snapshot version. 'Disable all public access' prominent, permission gated, server-enforced. Sharing wizard: type → audience → expiry → immutable version → exposure provider capabilities → review and approve. Explicit confirm that resource becomes internet-accessible. Agent requests show pending approval; no automatic activation.

## Operational states
Never claim exposure ACTIVE until provider observed route AND gateway successfully checks positive authorized delivery. Distinguish transport healthy from resource backend healthy. Surface private-only mode clearly.

## UX acceptance
No navigation reset when previewing; works narrow screens; all controls keyboard operable; no inline scripts from artifact source may access parent dashboard; screenshots aligned with current design are generated during implementation once current UI audited.
