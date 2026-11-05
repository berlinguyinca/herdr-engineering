# Private Memory Cognito implementation plan

Goal: add human authentication to existing private HTTPS dashboard Memory routes only; no deployment, secrets, grants, global authentication change, or new service.

Architecture: memory-specific `cognito` policy supplies one HTTPS origin and exact trusted proxy peer IPs; reuse central collab pool/client/group identifiers and SyncedDirectory. Cognito verifier requires access tokens and fresh group membership with no stale grace. Main dashboard bearer tokens never authenticate other resources. Existing explicitly scoped API keys continue unchanged; open-listener never grants Memory.

1. Backend RED/GREEN: real locally signed JWTs + HTTP server requests, including invalid/expired/issuer/client/use, duplicate Host/Origin/Authorization/Content-Type, invalid origins/ports, plaintext scheme mismatch, explicit proxy boundary, foreign identity/unlisted viewer/project/scope denial, no open-listener fallback, native API-key compatibility, fresh membership revocation. Implement `control_plane/memory_cognito.py`, memory-only API dispatch, and policy schema.
2. Frontend RED/GREEN: reuse collab PKCE + TokenStore with an isolated storage namespace. Same-origin HTTPS Memory-only requests, manual redirect rejection, no auth on non-Memory or external requests; callback state/origin validation and fixed return to Memory. Add sign-in/out to Memory view and callback dispatch before collab callback.
3. Document new host-only policy, explicit proxy trust, runtime dependency on #620, configuration reload requirement and no grants by default. Run appropriate Python/JS/UI checks, independent whole-diff security review, fix findings, then commit/push feature branch only.

Review focus: scheme/authority parsing, proxy peer identity, duplicate headers, JWT freshness, token leakage/redirects, PKCE callback confusion, no global auth changes.

## Execution evidence

- Existing Memory/Cognito baseline: 54 passed. Backend first RED: 15 failed; GET framing RED: 3 failed; both GREEN.
- Parent callback-isolation finding: RED then GREEN; independent reviewer reproduced and confirmed the fix.
- Independent security review: no Critical/auth bypass; native-key/bearer compatibility finding fixed RED→GREEN with real HTTP-gate client integration. Parent/reviewer canonical-port parity finding fixed RED→GREEN.
- Existing Memory Playwright fixture used open-listener: RED then explicitly scoped native-key fixture GREEN (desktop/mobile/keyboard). Fresh screenshots kept outside the PR.
- Frontend/client suite: 1,239 passed after review fixes. Repository-wide Python attempted: interrupted after 702 passed / 138 deselected at 203.8 seconds; focused API/auth suites run separately.
- No OpenViking migration work; user cancelled it. No automatic retention, production deployment, configuration grants, public routing, or network changes.
- Summary policy document cherry-picked from parent commit 62633d7a; runtime activation remains dependent on #620.

Final checks: 47 Memory Cognito cases passed, 229 focused API/auth compatibility cases passed, 1,239 client/component cases passed, and the real Chromium Memory UI test passed. Public listener denial and explicit native-key identity selection are exercised against real HTTP listeners. Ruff on new Python modules/tests and git diff --check passed. Live Cognito login and production activation are intentionally not performed in this slice.
