# Operator guide + adoption checklist — Herdr Personal Assistant (P1/P2 slice)

## Enable

The feature is off by default. To enable on the control plane, set the
`personal_assistant` config section:

```yaml
# herdr control-plane config
control_plane:
  personal_assistant:
    enabled: true
    approval_ttl_seconds: 3600
    grant_max_ttl_seconds: 2592000
```

When enabled, `/api/v1/health` advertises `assistant_v1`, which makes the
**Personal Assistant** nav entry appear. When disabled, the entry is hidden and
every assistant endpoint returns `503 {"code":"unavailable"}`; nothing runs.

## Use

1. **Settings → Personal Assistant** (or the sidebar entry) opens the assistant
   surface: assistant list, General/Permissions/Memory/Approvals/Diagnostics
   tabs, and a chat panel.
2. **General**: name, persona, instructions, model policy (`auto` = InferWeave
   routing), and the enable toggle. "Archive" soft-deletes and revokes grants.
3. **Permissions**: tools are listed with a *sensitive* flag. Click **Grant** to
   grant a tool for `scope=*` (deny-by-default otherwise). Sensitive tools
   (e.g. `send_message`, `run_shell`) require an approval unless you choose to
   pre-authorize when granting.
4. **Memory**: search, store corrected facts (provenance tracked), export to
   JSON, forget individual entries. Memory is isolated per user/assistant.
5. **Approvals**: approve or deny pending sensitive actions (fail-closed; an
   expired approval is treated as denied).
6. **Diagnostics**: runner, grants, approvals, thread/run/memory counts.

### Chat
Create a thread, send a message. The P1/P2 slice uses the deterministic
`LocalRunnerAdapter`, so replies are marked `[local-skeleton runner v1]` and are
**not** real model output. Wiring a real InferWeave runner is the P3 step.

## Adoption checklist
- [ ] Feature flag verified off by default (`assistant_config({}).enabled is False`).
- [ ] With assistant disabled, `/api/v1/assistants` returns 503 and no data is stored.
- [ ] Assistant CRUD, threads and chat work against a running control plane.
- [ ] Cross-user access to an assistant/thread returns 404.
- [ ] A tool with no grant is denied; a sensitive tool without approval is gated.
- [ ] Approving/denying an approval updates the linked action.
- [ ] Memory search/export/forget work and are isolated per user.
- [ ] UI renders at 1280px and 375px with the design tokens (screenshots in `screenshots/`).

## Not yet implemented (P3–P6) — see `PHASES.md`
Durable routines on the scheduler, real InferWeave inference, connectors /
computer tools / Pi-mission delegation, voice/mobile, production hardening.
These are out of scope for this slice and are not claimed as done.
