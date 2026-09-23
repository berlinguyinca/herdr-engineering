# Browser, Mobile and Integration Boundaries

Adopt verified existing plugins where possible.

Browser: real Chromium, generated/local site inspection, CDP/Playwright where supported, observation/takeover where supported, console/network evidence, screenshots/QA artifacts.

Mobile: iPhone/iPad/desktop web access, agent/session status, session timeline, supported terminal controls, browser/site preview, shared artifacts, approvals/review, robust reconnect.

Preferred network:
phone/laptop -> Tailscale -> HerdR web -> local capabilities.
Never automatically expose CDP/random dev ports/VNC/RDP/unauthenticated dashboards publicly.

Ownership:
- Pi Engineering: orchestration/policy/missions
- AutoSpec: spec/issue workflow
- Plannotator: review semantics
- HerdR: workspace/session host
- Powerpack: curated integration/distribution

Reuse swarm/worktree primitives where suitable. Session journal observes/summarizes workflow; it is not another workflow engine.

Do not enable task/Kanban/dispatch plugins by default until reconciled with:
AutoSpec -> GitHub Issues -> Pi Engineering Mission -> workers -> review -> completion.
