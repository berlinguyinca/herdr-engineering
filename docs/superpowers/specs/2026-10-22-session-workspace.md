# Session workspace

Approved direction: a session is a live, inspectable conversation with a separate activity pane. Collaboration and session inspection share rendering and composer behavior.

## Behavior

- Desktop has resizable conversation and activity panes; narrow screens have accessible tabs.
- Conversation shows chronological user/assistant messages with safe Markdown. Tool output is excluded; activity groups commands, outputs, changes, tests and usage with filters and collapsed output.
- Preserve reader position, open activity details, draft text and attachments through live refresh. Display explicit recording, delivery and truncation states.
- Shared composer supports Enter to send, Shift+Enter newline, IME, keyboard completion, agent/reference mentions and uploads with removal and errors. Collaboration retains its leading-agent task semantics. Session messages use the existing acknowledged native transport.
- Uploads cannot be advertised as delivered when native attachment transport is unsupported. Capability errors must be explicit and preserve the draft.
- Show helpers from explicit parent/agent associations, with navigation and filtering. GitHub links use explicit recorded issue/PR/commit identifiers and validated URLs; never infer ownership from cwd alone.
- Transcript readers use explicit native agent-session identity; never choose a transcript merely by matching cwd or newest modification time. Capture user/assistant text and tool events, exclude hidden reasoning and system/developer instructions. Incremental reading and replay are bounded and idempotent.
- Accept canonical percent-encoded pane identities. Persist redacted conversation messages and bounded coding activity/output through the existing acknowledged durable sink, with bounded retention. Do not persist hidden reasoning or system/developer instructions.

## Architecture

Reuse existing inspector ingestion, query/SSE and durable sink; extend the native feed with transcript adaptation. Extract reusable composer and safe message rendering for Collaboration and the session workspace. Keep Collaboration authentication and task permissions separate from operator session control.

## Validation

Regression tests cover real pane identities, durable message restore and redaction; transcript normalization, provenance and deduplication; safe rendering, helper/links and filters; completion, draft and delivery behavior. Run targeted Python and Node suites, then existing client/control-plane checks and browser coverage where available.

## Deployment boundary

The user explicitly authorized PR creation, review, merge and deployment. Roll the control-plane services and host collectors from the merged revision, preserving running native agents and their work. Do not send trial prompts to working sessions. Verify live recording coverage and report any remaining identity gaps explicitly.
