# Copy/paste startup prompt for Claude Sonnet / Pi Engineering

You are the implementing engineering agent on a REMOTE HERDR HOST. Integrate Rakazo capabilities natively into the existing Herdr Personal Assistant experience. Prioritize reuse, correctness, and tested delivery over rapid code duplication.

**Artifact transfer is mandatory.** This ZIP is downloaded onto my Mac, not automatically present on your remote host. Locate the Mac download and transfer it via an available authorized method (scp/rsync/SSH/same mounted path). Ask only if the Mac host/path is genuinely unreachable; do not pretend transfer worked. The expected filename is `herdr-rakazo-personal-assistant-spec.zip`. Example, adjust MAC_USER, MAC_HOST and project folder to discovered actual values:

```bash
# Run on remote execution host (assuming it can SSH into Mac):
mkdir -p /tmp/herdr-spec-transfer
scp 'MAC_USER@MAC_HOST:~/Downloads/herdr-rakazo-personal-assistant-spec.zip' /tmp/herdr-spec-transfer/
# Or run on Mac: scp ~/Downloads/herdr-rakazo-personal-assistant-spec.zip REMOTE_USER@REMOTE_HOST:/tmp/herdr-spec-transfer/
cd /path/to/herdr-engineering  # discover actual repo checkout
SPEC_NAME="rakazo-personal-assistant-$(date +%Y%m%d-%H%M%S)"
mkdir -p "docs/specs/$SPEC_NAME"
unzip -n /tmp/herdr-spec-transfer/herdr-rakazo-personal-assistant-spec.zip -d "docs/specs/$SPEC_NAME"
find "docs/specs/$SPEC_NAME" -maxdepth 2 -type f | sort
```

Then:
1. Read ALL spec files plus current `DESIGN.md`, `README.md`, `MASTER_SPEC.md`, `STATUS.md`, `HERDR_COMPATIBILITY.md`, and active local specs before changes. Validate bundle manifest and pinned source URLs.
2. Audit current working tree, branches and existing implementations in Herdr/Herdr Engineering/Pi Engineering/InferWeave/OpenViking, and Rakazo's current source; write `docs/specs/$SPEC_NAME/audit/current-state.md` with file/line/commit evidence. Do NOT assume current code state matches this snapshot.
3. Form explicit reuse/refactor/adapt/new mapping. No parallel orchestration, model router, auth, UI framework, artifact registry, scheduler or memory backend. Exclude Pi-Bolt and Pi Forge. Respect host blacklists; never automatically place on the Mac laptop.
4. Implement in order P0→P6, with bounded vertical slices, unit/contract/integration/E2E/security tests and document each gate. Use existing Herdr design tokens and routes; desktop/mobile screenshots for UI acceptance. Do not block all work waiting on optional voice/mobile enhancements.
5. Use safe independent branches/worktrees for concurrent implementation agents; review merges for duplicate work and conflicts. Follow existing repo CI/tests/build, no unapproved production deployment or destructive database actions.
6. Keep a living implementation ledger with decisions, exact modified files, tests and failures, remaining risks, and end-to-end demonstrations. Take action where permissions allow; ask for approval only for sensitive changes, credentials, external side effects or genuinely missing critical data.
7. Deliver tested native Settings → Personal Assistant, persistent assistant chat, policy-controlled tasks/approvals, memory and routines in phased PRs. Verify model/host routing, audit and failure recovery. Provide final operator guide and adoption checklist.

Every claimed capability must be traceable to an implemented endpoint/component and an acceptance test. Continue autonomously through the safely executable tasks and report blockers explicitly.
