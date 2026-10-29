# UX implementation contract

## Home
Top: title `Your teams`; short team cards with project, current goal, small number of active tasks, blocked/ready counts, last meaningful status. Only visible attention requiring user action. No raw host/GPU chart.

## Team room
Two primary tabs: `Conversation` (default) and `Team Map`. Conversation: messages, one-card parent task progress, artifact inline summary, single composer supporting plain text and attachments. Buttons `Ask team`, `See progress`, `Review results` appear contextually; do not force explicit model or agent selection.

## Team Map
Project -> issue -> task -> artifacts; agents connected via labeled assignment/review edges. Distinguish persistent historical facts from ephemeral presence. Node click opens side inspector (purpose/status/owner/evidence/artifacts/decisions). Grouping, collapsing, low-noise, search, layout persistence and keyboard accessible text equivalent. Shared default layout and user-specific overrides. Revisit old closed work unchanged.

## Pi
`/rooms`, `/room join <name>`, `/room catchup`, `/room tasks`, `/room artifacts`; `pi room list|attach|history|leave`; direct `@agent` optional. Simple vs activity vs expert verbosity; terminal accessible keyboard navigation and clear reconnect notices. Always preserve existing Pi workflows.

## Approval
One actionable card showing requested action, consequences, actor, exact target and expiry, evidence. Approve/reject from any client; decision reflected everywhere. No duplicates.

## Avoid
Separate agent and assistant conversations by default, persistent toast noise, unexplained acronyms, fabricated progress %, model/host controls on main path, giant unfiltered graphs, automatic redirects, duplicate artifact registries.
